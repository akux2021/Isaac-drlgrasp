# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

from pickle import EMPTY_TUPLE
from einops.einops import rearrange
import numpy as np
import os

from isaacgym import gymtorch
from isaacgym import gymapi
from rlgpu.utils.torch_jit_utils import *
from rlgpu.tasks.base.base_task import BaseTask
import torch

import matplotlib
import matplotlib.pyplot as plt
from PIL import Image as Im
import math
from einops.layers.torch import Rearrange, Reduce
from .demonstration import Demonstration


class BaxterCabinet(BaseTask):

    def __init__(self, cfg, sim_params, physics_engine, device_type, device_id, headless):
        self.cfg = cfg
        self.sim_params = sim_params
        self.physics_engine = physics_engine

        self.max_episode_length = self.cfg["env"]["episodeLength"]

        self.action_scale = self.cfg["env"]["actionScale"]
        self.start_position_noise = self.cfg["env"]["startPositionNoise"]
        self.start_rotation_noise = self.cfg["env"]["startRotationNoise"]
        self.num_props = self.cfg["env"]["numProps"]
        self.aggregate_mode = self.cfg["env"]["aggregateMode"]

        self.dof_vel_scale = self.cfg["env"]["dofVelocityScale"]
        self.dist_reward_scale = self.cfg["env"]["distRewardScale"]
        self.rot_reward_scale = self.cfg["env"]["rotRewardScale"]
        self.around_handle_reward_scale = self.cfg["env"]["aroundHandleRewardScale"]
        self.open_reward_scale = self.cfg["env"]["openRewardScale"]
        self.finger_dist_reward_scale = self.cfg["env"]["fingerDistRewardScale"]
        self.action_penalty_scale = self.cfg["env"]["actionPenaltyScale"]

        self.debug_viz = self.cfg["env"]["enableDebugVis"]

        self.up_axis = "z"
        self.up_axis_idx = 2

        self.distX_offset = -0.7
        self.dt = 1/60.

        # prop dimensions
        self.prop_width = 0.08
        self.prop_height = 0.08
        self.prop_length = 0.08
        self.prop_spacing = 0.09

        self.num_obs = 13
        self.num_acts = 9
        self.baxter_begin_dof = 10

        self.cfg["env"]["numObservations"] = self.num_obs
        self.cfg["env"]["numActions"] = self.num_acts

        self.cfg["device_type"] = device_type
        self.cfg["device_id"] = device_id
        self.cfg["headless"] = headless

        # Camera Sensor
        self.camera_props = gymapi.CameraProperties()
        self.camera_props.width = 128
        self.camera_props.height = 128
        self.camera_props.enable_tensors = True
        self.debug_fig = plt.figure("debug")

        self.demonstration = Demonstration('/home/lohse/isaac_ws/src/isaac-gym/scripts/Isaac-drlgrasp/envs_test/npresult1.txt')
        self.demostration_round = 0
        self.demostration_step = 0

        super().__init__(cfg=self.cfg)

        # get gym GPU state tensors
        actor_root_state_tensor = self.gym.acquire_actor_root_state_tensor(self.sim)
        dof_state_tensor = self.gym.acquire_dof_state_tensor(self.sim)
        rigid_body_tensor = self.gym.acquire_rigid_body_state_tensor(self.sim)

        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        # create some wrapper tensors for different slices
        self.baxter_default_dof_pos = to_torch([0, 0, -1.57, 0, 2.5, 0, 0, 0, 0, 0, 1.2272, -1.3570,  0.0937,  2.2918,  0.6131, -1.0368,  1.2078,  0.0200, -0.0200], device=self.device)
        self.dof_state = gymtorch.wrap_tensor(dof_state_tensor)
        self.baxter_dof_state = self.dof_state.view(self.num_envs, -1, 2)[:, :self.num_baxter_dofs]
        self.baxter_dof_pos = self.baxter_dof_state[..., 0]
        self.baxter_dof_vel = self.baxter_dof_state[..., 1]
        self.cabinet_dof_state = self.dof_state.view(self.num_envs, -1, 2)[:, self.num_baxter_dofs:]
        self.cabinet_dof_pos = self.cabinet_dof_state[..., 0]
        self.cabinet_dof_vel = self.cabinet_dof_state[..., 1]
        self.retrieved_index = [11, 12, 13, 14, 15, 16, 17, 18, 19]

        self.rigid_body_states = gymtorch.wrap_tensor(rigid_body_tensor).view(self.num_envs, -1, 13)
        self.num_bodies = self.rigid_body_states.shape[1]

        self.root_state_tensor = gymtorch.wrap_tensor(actor_root_state_tensor).view(self.num_envs, -1, 13)

        if self.num_props > 0:
            self.prop_states = self.root_state_tensor[:, 2:]

        self.num_dofs = self.gym.get_sim_dof_count(self.sim) // self.num_envs
        self.baxter_dof_targets = torch.zeros((self.num_envs, self.num_dofs), dtype=torch.float, device=self.device)

        self.global_indices = torch.arange(self.num_envs * (2 + self.num_props), dtype=torch.int32, device=self.device).view(self.num_envs, -1)

        self.reset(torch.arange(self.num_envs, device=self.device))

    def create_sim(self):
        self.sim_params.up_axis = gymapi.UP_AXIS_Z
        self.sim_params.gravity.x = 0
        self.sim_params.gravity.y = 0
        self.sim_params.gravity.z = -9.81
        self.sim_params.physx.solver_type = 1
        self.sim_params.physx.num_position_iterations = 150
        self.sim_params.physx.num_velocity_iterations = 25
        self.sim = super().create_sim(
            self.device_id, self.graphics_device_id, self.physics_engine, self.sim_params)
        self._create_ground_plane()
        self._create_envs(self.num_envs, self.cfg["env"]['envSpacing'], int(np.sqrt(self.num_envs)))

    def _create_ground_plane(self):
        plane_params = gymapi.PlaneParams()
        plane_params.normal = gymapi.Vec3(0.0, 0.0, 1.0)
        self.gym.add_ground(self.sim, plane_params)

    def _create_envs(self, num_envs, spacing, num_per_row):
        lower = gymapi.Vec3(-spacing, -spacing, 0.0)
        upper = gymapi.Vec3(spacing, spacing, spacing)

        asset_root = "/home/lohse/isaac_ws/src/isaac-gym/scripts/Isaac-drlgrasp/assets"
        baxter_asset_file = "baxter/baxter_isaac.urdf"
        cabinet_asset_file = "urdf/sektion_cabinet_model/urdf/sektion_cabinet_2.urdf"

        # if "asset" in self.cfg["env"]:
        #     asset_root = self.cfg["env"]["asset"].get("assetRoot", asset_root)
        #     baxter_asset_file = self.cfg["env"]["asset"].get("assetFileNamebaxter", baxter_asset_file)
        #     cabinet_asset_file = self.cfg["env"]["asset"].get("assetFileNameCabinet", cabinet_asset_file)

        # load baxter asset
        asset_options = gymapi.AssetOptions()
        asset_options.flip_visual_attachments = True
        asset_options.fix_base_link = True
        asset_options.collapse_fixed_joints = True
        asset_options.disable_gravity = True
        asset_options.thickness = 0.0001
        asset_options.default_dof_drive_mode = gymapi.DOF_MODE_POS
        asset_options.use_mesh_materials = True
        asset_options.mesh_normal_mode = gymapi.COMPUTE_PER_VERTEX
        asset_options.use_mesh_materials = True
        asset_options.override_com = True
        asset_options.override_inertia = True
        asset_options.vhacd_enabled = True
        asset_options.vhacd_params = gymapi.VhacdParams()
        asset_options.vhacd_params.resolution = 500000
        baxter_asset = self.gym.load_asset(self.sim, asset_root, baxter_asset_file, asset_options)

        # load cabinet asset
        asset_options.flip_visual_attachments = False
        asset_options.collapse_fixed_joints = True
        asset_options.disable_gravity = False
        asset_options.default_dof_drive_mode = gymapi.DOF_MODE_NONE
        asset_options.armature = 0.005
        asset_options.thickness = 0.0001
        asset_options.mesh_normal_mode = gymapi.COMPUTE_PER_VERTEX
        asset_options.use_mesh_materials = True
        asset_options.override_com = True
        asset_options.override_inertia = True
        asset_options.vhacd_enabled = True
        asset_options.vhacd_params = gymapi.VhacdParams()
        asset_options.vhacd_params.resolution = 1000000
        cabinet_asset = self.gym.load_asset(self.sim, asset_root, cabinet_asset_file, asset_options)

        baxter_dof_stiffness = to_torch([400, 400, 400, 400, 400, 400, 400, 1.0e4, 1.0e4, 1.0e4, 1.0e4, 1.0e4, 400, 400, 400, 400, 400, 400, 400], dtype=torch.float, device=self.device)
        baxter_dof_damping = to_torch([80, 80, 80, 80, 80, 80, 80, 1.0e2, 1.0e2, 1.0e2, 1.0e2, 1.0e2, 80, 80, 80, 80, 80, 80, 80], dtype=torch.float, device=self.device)
        baxter_dof_lower_limit = to_torch([-3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14, -3.14], dtype=torch.float, device=self.device)
        baxter_dof_upper_limit = to_torch([3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14, 3.14], dtype=torch.float, device=self.device)

        self.num_baxter_bodies = self.gym.get_asset_rigid_body_count(baxter_asset)
        self.num_baxter_dofs = self.gym.get_asset_dof_count(baxter_asset)
        self.num_cabinet_bodies = self.gym.get_asset_rigid_body_count(cabinet_asset)
        self.num_cabinet_dofs = self.gym.get_asset_dof_count(cabinet_asset)

        print("num baxter bodies: ", self.num_baxter_bodies)
        print("num baxter dofs: ", self.num_baxter_dofs)
        print("num cabinet bodies: ", self.num_cabinet_bodies)
        print("num cabinet dofs: ", self.num_cabinet_dofs)

        # set baxter dof properties
        baxter_dof_props = self.gym.get_asset_dof_properties(baxter_asset)
        self.baxter_dof_lower_limits = []
        self.baxter_dof_upper_limits = []
        for i in range(self.num_baxter_dofs):
            baxter_dof_props['driveMode'][i] = gymapi.DOF_MODE_POS
            if self.physics_engine == gymapi.SIM_PHYSX:
                baxter_dof_props['stiffness'][i] = baxter_dof_props['stiffness'][i]
                baxter_dof_props['damping'][i] = baxter_dof_props['damping'][i]
            else:
                baxter_dof_props['stiffness'][i] = baxter_dof_props['stiffness'][i]
                baxter_dof_props['damping'][i] = baxter_dof_props['damping'][i]

            self.baxter_dof_lower_limits.append(baxter_dof_props['lower'][i])
            self.baxter_dof_upper_limits.append(baxter_dof_props['upper'][i])

        self.baxter_ranges = baxter_dof_props['lower'] - baxter_dof_props['upper']
        self.baxter_mids = 0.5 * (baxter_dof_props['upper'] + baxter_dof_props['lower'])
        baxter_num_dofs = len(baxter_dof_props)

        # set default DOF states
        self.default_dof_state = np.zeros(baxter_num_dofs, gymapi.DofState.dtype)
        self.default_dof_state["pos"] = self.baxter_mids
        self.baxter_dof_lower_limits = to_torch(self.baxter_dof_lower_limits, device=self.device)
        self.baxter_dof_upper_limits = to_torch(self.baxter_dof_upper_limits, device=self.device)
        self.baxter_dof_speed_scales = torch.ones_like(self.baxter_dof_lower_limits)
        self.baxter_dof_speed_scales[[17, 18]] = 1

        # set cabinet dof properties
        cabinet_dof_props = self.gym.get_asset_dof_properties(cabinet_asset)
        for i in range(self.num_cabinet_dofs):
            cabinet_dof_props['damping'][i] = 10.0
        
        # create prop assets
        box_opts = gymapi.AssetOptions()
        box_opts.density = 400
        prop_asset = self.gym.create_box(self.sim, self.prop_width, self.prop_height, self.prop_width, box_opts)

        baxter_start_pose = gymapi.Transform()
        baxter_start_pose.p = gymapi.Vec3(1.4, 0.0, 1.0)
        baxter_start_pose.r = gymapi.Quat(0.0, 0.0, 1.0, 0.0)

        cabinet_start_pose = gymapi.Transform()
        cabinet_start_pose.p = gymapi.Vec3(*get_axis_params(1.1, self.up_axis_idx))

        # compute aggregate size
        num_baxter_bodies = self.gym.get_asset_rigid_body_count(baxter_asset)
        num_baxter_shapes = self.gym.get_asset_rigid_shape_count(baxter_asset)
        num_cabinet_bodies = self.gym.get_asset_rigid_body_count(cabinet_asset)
        num_cabinet_shapes = self.gym.get_asset_rigid_shape_count(cabinet_asset)
        num_prop_bodies = self.gym.get_asset_rigid_body_count(prop_asset)
        num_prop_shapes = self.gym.get_asset_rigid_shape_count(prop_asset)
        max_agg_bodies = num_baxter_bodies + num_cabinet_bodies + self.num_props * num_prop_bodies
        max_agg_shapes = num_baxter_shapes + num_cabinet_shapes + self.num_props * num_prop_shapes

        self.baxters = []
        self.cabinets = []
        self.default_prop_states = []
        self.prop_start = []
        self.envs = []
        self.camera_handles = []

        for i in range(self.num_envs):
            # create env instance
            env_ptr = self.gym.create_env(
                self.sim, lower, upper, num_per_row
            )
            camera_handle = self.gym.create_camera_sensor(env_ptr, self.camera_props)
            transform = gymapi.Transform()
            transform.p = gymapi.Vec3(1.25, 0, 1.2)
            transform.r = gymapi.Quat.from_euler_zyx(math.pi, 0.75 * math.pi, 0)
            self.gym.set_camera_transform(camera_handle, env_ptr, transform)

            if self.aggregate_mode >= 3:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            baxter_actor = self.gym.create_actor(env_ptr, baxter_asset, baxter_start_pose, "baxter", i, 1, 0)
            
            # Set initial DOF states
            self.gym.set_actor_dof_states(env_ptr, baxter_actor, self.default_dof_state, gymapi.STATE_ALL)
            
            self.gym.set_actor_dof_properties(env_ptr, baxter_actor, baxter_dof_props)

            if self.aggregate_mode == 2:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            cabinet_pose = cabinet_start_pose
            cabinet_pose.p.x += self.start_position_noise * (np.random.rand() - 0.5)
            dz = 0.5 * np.random.rand()
            dy = np.random.rand() - 0.5
            cabinet_pose.p.y += self.start_position_noise * dy
            cabinet_pose.p.z += self.start_position_noise * dz
            cabinet_actor = self.gym.create_actor(env_ptr, cabinet_asset, cabinet_pose, "cabinet", i, 2, 0)
            self.gym.set_actor_dof_properties(env_ptr, cabinet_actor, cabinet_dof_props)

            if self.aggregate_mode == 1:
                self.gym.begin_aggregate(env_ptr, max_agg_bodies, max_agg_shapes, True)

            if self.num_props > 0:
                self.prop_start.append(self.gym.get_sim_actor_count(self.sim))
                drawer_handle = self.gym.find_actor_rigid_body_handle(env_ptr, cabinet_actor, "drawer_top")
                drawer_pose = self.gym.get_rigid_transform(env_ptr, drawer_handle)

                props_per_row = int(np.ceil(np.sqrt(self.num_props)))
                xmin = -0.5 * self.prop_spacing * (props_per_row - 1)
                yzmin = -0.5 * self.prop_spacing * (props_per_row - 1)

                prop_count = 0
                for j in range(props_per_row):
                    prop_up = yzmin + j * self.prop_spacing
                    for k in range(props_per_row):
                        if prop_count >= self.num_props:
                            break
                        propx = xmin + k * self.prop_spacing
                        prop_state_pose = gymapi.Transform()
                        prop_state_pose.p.x = drawer_pose.p.x + propx
                        propz, propy = 0, prop_up
                        prop_state_pose.p.y = drawer_pose.p.y + propy
                        prop_state_pose.p.z = drawer_pose.p.z + propz
                        prop_state_pose.r = gymapi.Quat(0, 0, 0, 1)
                        prop_handle = self.gym.create_actor(env_ptr, prop_asset, prop_state_pose, "prop{}".format(prop_count), i, 0, 0)
                        prop_count += 1

                        prop_idx = j * props_per_row + k
                        self.default_prop_states.append([prop_state_pose.p.x, prop_state_pose.p.y, prop_state_pose.p.z,
                                                         prop_state_pose.r.x, prop_state_pose.r.y, prop_state_pose.r.z, prop_state_pose.r.w,
                                                         0, 0, 0, 0, 0, 0])
            if self.aggregate_mode > 0:
                self.gym.end_aggregate(env_ptr)

            self.envs.append(env_ptr)
            self.baxters.append(baxter_actor)
            self.cabinets.append(cabinet_actor)
            self.camera_handles.append(camera_handle)
            
        self.hand_handle = self.gym.find_actor_rigid_body_handle(env_ptr, baxter_actor, "right_wrist")
        self.drawer_handle = self.gym.find_actor_rigid_body_handle(env_ptr, cabinet_actor, "drawer_top")
        self.lfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, baxter_actor, "r_gripper_l_finger")
        self.rfinger_handle = self.gym.find_actor_rigid_body_handle(env_ptr, baxter_actor, "r_gripper_r_finger")
        self.default_prop_states = to_torch(self.default_prop_states, device=self.device, dtype=torch.float).view(self.num_envs, self.num_props, 13)

        self.init_data()

    def init_data(self):
        hand = self.gym.find_actor_rigid_body_handle(self.envs[0], self.baxters[0], "right_wrist")
        lfinger = self.gym.find_actor_rigid_body_handle(self.envs[0], self.baxters[0], "r_gripper_l_finger")
        rfinger = self.gym.find_actor_rigid_body_handle(self.envs[0], self.baxters[0], "r_gripper_r_finger")

        hand_pose = self.gym.get_rigid_transform(self.envs[0], hand)
        lfinger_pose = self.gym.get_rigid_transform(self.envs[0], lfinger)
        rfinger_pose = self.gym.get_rigid_transform(self.envs[0], rfinger)

        finger_pose = gymapi.Transform()
        finger_pose.p = (lfinger_pose.p + rfinger_pose.p) * 0.5
        finger_pose.r = lfinger_pose.r

        hand_pose_inv = hand_pose.inverse()
        grasp_pose_axis = 1
        baxter_local_grasp_pose = hand_pose_inv * finger_pose
        # baxter_local_grasp_pose = hand_pose
        baxter_local_grasp_pose.p += gymapi.Vec3(*get_axis_params(0.04, grasp_pose_axis))
        self.baxter_local_grasp_pos = to_torch([baxter_local_grasp_pose.p.x, baxter_local_grasp_pose.p.y,
                                                baxter_local_grasp_pose.p.z], device=self.device).repeat((self.num_envs, 1))
        self.baxter_local_grasp_rot = to_torch([baxter_local_grasp_pose.r.x, baxter_local_grasp_pose.r.y,
                                                baxter_local_grasp_pose.r.z, baxter_local_grasp_pose.r.w], device=self.device).repeat((self.num_envs, 1))

        drawer_local_grasp_pose = gymapi.Transform()
        drawer_local_grasp_pose.p = gymapi.Vec3(*get_axis_params(0.01, grasp_pose_axis, 0.3))
        drawer_local_grasp_pose.r = gymapi.Quat(0, 0, 0, 1)
        self.drawer_local_grasp_pos = to_torch([drawer_local_grasp_pose.p.x, drawer_local_grasp_pose.p.y,
                                                drawer_local_grasp_pose.p.z], device=self.device).repeat((self.num_envs, 1))
        self.drawer_local_grasp_rot = to_torch([drawer_local_grasp_pose.r.x, drawer_local_grasp_pose.r.y,
                                                drawer_local_grasp_pose.r.z, drawer_local_grasp_pose.r.w], device=self.device).repeat((self.num_envs, 1))

        self.gripper_forward_axis = to_torch([0, 0, 1], device=self.device).repeat((self.num_envs, 1))
        self.drawer_inward_axis = to_torch([-1, 0, 0], device=self.device).repeat((self.num_envs, 1))
        self.gripper_up_axis = to_torch([0, 1, 0], device=self.device).repeat((self.num_envs, 1))
        self.drawer_up_axis = to_torch([0, 0, 1], device=self.device).repeat((self.num_envs, 1))

        self.baxter_grasp_pos = torch.zeros_like(self.baxter_local_grasp_pos)
        self.baxter_grasp_rot = torch.zeros_like(self.baxter_local_grasp_rot)
        self.baxter_grasp_rot[..., -1] = 1  # xyzw
        self.drawer_grasp_pos = torch.zeros_like(self.drawer_local_grasp_pos)
        self.drawer_grasp_rot = torch.zeros_like(self.drawer_local_grasp_rot)
        self.drawer_grasp_rot[..., -1] = 1
        self.baxter_lfinger_pos = torch.zeros_like(self.baxter_local_grasp_pos)
        self.baxter_rfinger_pos = torch.zeros_like(self.baxter_local_grasp_pos)
        self.baxter_lfinger_rot = torch.zeros_like(self.baxter_local_grasp_rot)
        self.baxter_rfinger_rot = torch.zeros_like(self.baxter_local_grasp_rot)

    def compute_reward(self, actions):
        self.rew_buf[:], self.reset_buf[:] = compute_baxter_reward(
            self.reset_buf, self.progress_buf, self.actions, self.cabinet_dof_pos,
            self.baxter_grasp_pos, self.drawer_grasp_pos, self.baxter_grasp_rot, self.drawer_grasp_rot,
            self.baxter_lfinger_pos, self.baxter_rfinger_pos, self.goal_buf,
            self.gripper_forward_axis, self.drawer_inward_axis, self.gripper_up_axis, self.drawer_up_axis,
            self.num_envs, self.dist_reward_scale, self.rot_reward_scale, self.around_handle_reward_scale, self.open_reward_scale,
            self.finger_dist_reward_scale, self.action_penalty_scale, self.distX_offset, self.max_episode_length
        )

    def compute_observations(self):

        self.gym.refresh_actor_root_state_tensor(self.sim)
        self.gym.refresh_dof_state_tensor(self.sim)
        self.gym.refresh_rigid_body_state_tensor(self.sim)

        self.hand_pos = self.rigid_body_states[:, self.hand_handle][:, 0:3]
        self.hand_rot = self.rigid_body_states[:, self.hand_handle][:, 3:7]        
        drawer_pos = self.rigid_body_states[:, self.drawer_handle][:, 0:3]
        drawer_rot = self.rigid_body_states[:, self.drawer_handle][:, 3:7]

        # self.baxter_lfinger_pos = self.rigid_body_states[:, self.lfinger_handle][:, 0:3] + to_torch([0.0, -0.01725, 0.1127], device=self.device)
        self.baxter_lfinger_pos = self.rigid_body_states[:, self.lfinger_handle][:, 0:3] * 1.55 - self.hand_pos * 0.55
        self.baxter_rfinger_pos = self.rigid_body_states[:, self.rfinger_handle][:, 0:3] * 1.55 - self.hand_pos * 0.55
        self.baxter_lfinger_rot = self.rigid_body_states[:, self.lfinger_handle][:, 3:7]
        self.baxter_rfinger_rot = self.rigid_body_states[:, self.rfinger_handle][:, 3:7]

        self.baxter_grasp_rot[:], self.baxter_grasp_pos[:], self.drawer_grasp_rot[:], self.drawer_grasp_pos[:] = \
            compute_grasp_transforms(self.hand_rot, self.hand_pos, self.baxter_local_grasp_rot, self.baxter_local_grasp_pos,
                                     drawer_rot, drawer_pos, self.drawer_local_grasp_rot, self.drawer_local_grasp_pos
                                     )

        self.baxter_grasp_pos[:] = (self.baxter_lfinger_pos + self.baxter_rfinger_pos) / 2.0

        dof_pos_scaled = (2.0 * (self.baxter_dof_pos - self.baxter_dof_lower_limits)
                          / (self.baxter_dof_upper_limits - self.baxter_dof_lower_limits) - 1.0)

        to_target = self.drawer_grasp_pos - self.baxter_grasp_pos

        # num: 12 + 12 + 3 + 1 + 1
        self.obs_buf = torch.cat((dof_pos_scaled[:, self.baxter_begin_dof:19], to_target,
                                  self.cabinet_dof_pos[:, 3].unsqueeze(-1)), dim=-1)

        self.goal_buf = torch.cat((to_target, self.cabinet_dof_pos[:, 3].unsqueeze(-1)), dim=-1)

        #visual input
        # camera_tensor = self.gym.get_camera_image_gpu_tensor(self.sim, self.envs[0], self.camera_handles[0], gymapi.IMAGE_COLOR)
        # torch_camera_tensor = gymtorch.wrap_tensor(camera_tensor)
        # torch_camera_tensor = to_torch(torch_camera_tensor, dtype=torch.float, device=self.device).unsqueeze(0)

        # self.img_buf = torch_camera_tensor
        # for i in range(1, self.num_envs):
        #     camera_tensor = self.gym.get_camera_image_gpu_tensor(self.sim, self.envs[i], self.camera_handles[i], gymapi.IMAGE_COLOR)
        #     torch_camera_tensor = gymtorch.wrap_tensor(camera_tensor)
        #     torch_camera_tensor = to_torch(torch_camera_tensor, dtype=torch.float, device=self.device).unsqueeze(0)
        #     self.img_buf = torch.cat((self.img_buf, torch_camera_tensor), dim=0)

        # self.img_buf = self.img_buf[:, :, :, :3]
        # #image scale and normalize
        # image_mean = [0.485, 0.456, 0.406]
        # image_std = [0.229, 0.224, 0.225]
        # self.img_buf = self.img_buf / 255
        # for c in range(3):
        #     self.img_buf[:, :, c] = (self.img_buf[:, :, c] - image_mean[c])/image_std[c]

        # # add aux obs
        # self.img_buf = rearrange(self.img_buf, 'b h w c -> b (h w c)')
        # self.obs_buf = torch.cat((self.img_buf, dof_pos_scaled), dim=1)
        return self.obs_buf

    def reset(self, env_ids):
        env_ids_int32 = env_ids.to(dtype=torch.int32)

        # reset baxter
        pos = tensor_clamp(
            self.baxter_default_dof_pos.unsqueeze(0) + 0.25 * (torch.rand((len(env_ids), self.num_baxter_dofs), device=self.device) - 0.5),
            self.baxter_dof_lower_limits, self.baxter_dof_upper_limits)
        self.baxter_dof_pos[env_ids, :] = pos
        self.baxter_dof_vel[env_ids, :] = torch.zeros_like(self.baxter_dof_vel[env_ids])
        self.baxter_dof_targets[env_ids, :self.num_baxter_dofs] = pos

        # reset cabinet
        self.cabinet_dof_state[env_ids, :] = torch.zeros_like(self.cabinet_dof_state[env_ids])

        # reset props
        if self.num_props > 0:
            prop_indices = self.global_indices[env_ids, 2:].flatten()
            self.prop_states[env_ids] = self.default_prop_states[env_ids]
            self.gym.set_actor_root_state_tensor_indexed(self.sim,
                                                         gymtorch.unwrap_tensor(self.root_state_tensor),
                                                         gymtorch.unwrap_tensor(prop_indices), len(prop_indices))

        multi_env_ids_int32 = self.global_indices[env_ids, :2].flatten()
        self.gym.set_dof_position_target_tensor_indexed(self.sim,
                                                        gymtorch.unwrap_tensor(self.baxter_dof_targets),
                                                        gymtorch.unwrap_tensor(multi_env_ids_int32), len(multi_env_ids_int32))

        self.gym.set_dof_state_tensor_indexed(self.sim,
                                              gymtorch.unwrap_tensor(self.dof_state),
                                              gymtorch.unwrap_tensor(multi_env_ids_int32), len(multi_env_ids_int32))

        self.reverse_actions = self.baxter_dof_targets[:, self.baxter_begin_dof:self.num_baxter_dofs]
        
        self.progress_buf[env_ids] = 0
        self.reset_buf[env_ids] = 0
        self.demostration_step = 0
        self.demostration_round += 1

    def pre_physics_step(self, actions):
        if self.demostration_round < 1:
            self.actions = self.demostration.get_dof_pos(self.demostration_step)
            self.actions = self.actions.to(self.device)
            self.demostration_step += 1

            self.baxter_dof_targets[:, :] = self.actions
            self.gym.set_dof_position_target_tensor(self.sim,
                                                    gymtorch.unwrap_tensor(self.baxter_dof_targets))

            # reverse inference action
            if self.demostration_step == 1:
                self.pos_cur_last = self.baxter_dof_targets[:, self.baxter_begin_dof:self.num_baxter_dofs]

            elif self.demostration_step > 1:
                pos_cur = self.baxter_dof_targets[:, self.baxter_begin_dof:self.num_baxter_dofs]
                
                self.reverse_actions = (pos_cur - self.pos_cur_last) / (self.dt * self.action_scale)

                self.pos_cur_last = pos_cur.clone()

            if self.demostration_step == self.demostration.step_size:
                self.reset_buf = torch.ones_like(self.reset_buf)
        else:
            self.actions = actions.clone().to(self.device)

            targets = self.baxter_dof_targets[:, self.baxter_begin_dof:19] + self.dt * self.actions[:, :9] * self.action_scale
            self.baxter_dof_targets[:, self.baxter_begin_dof:19] = tensor_clamp(
                targets, self.baxter_dof_lower_limits[self.baxter_begin_dof:19], self.baxter_dof_upper_limits[self.baxter_begin_dof:19])
            
            # for i in range(self.num_envs):
            #     if self.actions[i, 7] > 0.0:
            #         self.baxter_dof_targets[i, 17] = 0.02
            #         self.baxter_dof_targets[i, 18] = -0.02
            #         self.gripper_flag = to_torch([1], dtype=torch.float, device=self.device).repeat((self.num_envs, 1))

            #     else:
            #         self.baxter_dof_targets[i, 17] = 0.0
            #         self.baxter_dof_targets[i, 18] = 0.0
            #         self.gripper_flag = to_torch([-1], dtype=torch.float, device=self.device).repeat((self.num_envs, 1))

            
            env_ids_int32 = torch.arange(self.num_envs, dtype=torch.int32, device=self.device)
            self.gym.set_dof_position_target_tensor(self.sim,
                                                    gymtorch.unwrap_tensor(self.baxter_dof_targets))

    def post_physics_step(self):
        self.progress_buf += 1

        env_ids = self.reset_buf.nonzero(as_tuple=False).squeeze(-1)
        if len(env_ids) > 0:
            self.reset(env_ids)

        self.gym.render_all_camera_sensors(self.sim)
        self.gym.start_access_image_tensors(self.sim)
        self.compute_observations()
        self.compute_reward(self.actions)

        # debug viz
        if self.viewer and self.debug_viz:
            self.gym.clear_lines(self.viewer)
            self.gym.refresh_rigid_body_state_tensor(self.sim)

            for i in range(self.num_envs):
                px = (self.baxter_grasp_pos[i] + quat_apply(self.baxter_grasp_rot[i], to_torch([1, 0, 0], device=self.device) * 0.2)).cpu().numpy()
                py = (self.baxter_grasp_pos[i] + quat_apply(self.baxter_grasp_rot[i], to_torch([0, 1, 0], device=self.device) * 0.2)).cpu().numpy()
                pz = (self.baxter_grasp_pos[i] + quat_apply(self.baxter_grasp_rot[i], to_torch([0, 0, 1], device=self.device) * 0.2)).cpu().numpy()

                p0 = self.baxter_grasp_pos[i].cpu().numpy()
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], px[0], px[1], px[2]], [0.85, 0.1, 0.1])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], py[0], py[1], py[2]], [0.1, 0.85, 0.1])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], pz[0], pz[1], pz[2]], [0.1, 0.1, 0.85])

                px = (self.drawer_grasp_pos[i] + quat_apply(self.drawer_grasp_rot[i], to_torch([1, 0, 0], device=self.device) * 0.2)).cpu().numpy()
                py = (self.drawer_grasp_pos[i] + quat_apply(self.drawer_grasp_rot[i], to_torch([0, 1, 0], device=self.device) * 0.2)).cpu().numpy()
                pz = (self.drawer_grasp_pos[i] + quat_apply(self.drawer_grasp_rot[i], to_torch([0, 0, 1], device=self.device) * 0.2)).cpu().numpy()

                p0 = self.drawer_grasp_pos[i].cpu().numpy()
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], px[0], px[1], px[2]], [1, 0, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], py[0], py[1], py[2]], [0, 1, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], pz[0], pz[1], pz[2]], [0, 0, 1])

                px = (self.baxter_lfinger_pos[i] + quat_apply(self.baxter_lfinger_rot[i], to_torch([1, 0, 0], device=self.device) * 0.2)).cpu().numpy()
                py = (self.baxter_lfinger_pos[i] + quat_apply(self.baxter_lfinger_rot[i], to_torch([0, 1, 0], device=self.device) * 0.2)).cpu().numpy()
                pz = (self.baxter_lfinger_pos[i] + quat_apply(self.baxter_lfinger_rot[i], to_torch([0, 0, 1], device=self.device) * 0.2)).cpu().numpy()

                p0 = self.baxter_lfinger_pos[i].cpu().numpy()
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], px[0], px[1], px[2]], [1, 0, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], py[0], py[1], py[2]], [0, 1, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], pz[0], pz[1], pz[2]], [0, 0, 1])

                px = (self.baxter_rfinger_pos[i] + quat_apply(self.baxter_rfinger_rot[i], to_torch([1, 0, 0], device=self.device) * 0.2)).cpu().numpy()
                py = (self.baxter_rfinger_pos[i] + quat_apply(self.baxter_rfinger_rot[i], to_torch([0, 1, 0], device=self.device) * 0.2)).cpu().numpy()
                pz = (self.baxter_rfinger_pos[i] + quat_apply(self.baxter_rfinger_rot[i], to_torch([0, 0, 1], device=self.device) * 0.2)).cpu().numpy()

                p0 = self.baxter_rfinger_pos[i].cpu().numpy()
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], px[0], px[1], px[2]], [1, 0, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], py[0], py[1], py[2]], [0, 1, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], pz[0], pz[1], pz[2]], [0, 0, 1])

                px = (self.hand_pos[i] + quat_apply(self.hand_rot[i], to_torch([1, 0, 0], device=self.device) * 0.2)).cpu().numpy()
                py = (self.hand_pos[i] + quat_apply(self.hand_rot[i], to_torch([0, 1, 0], device=self.device) * 0.2)).cpu().numpy()
                pz = (self.hand_pos[i] + quat_apply(self.hand_rot[i], to_torch([0, 0, 1], device=self.device) * 0.2)).cpu().numpy()

                p0 = self.hand_pos[i].cpu().numpy()
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], px[0], px[1], px[2]], [1, 0, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], py[0], py[1], py[2]], [0, 1, 0])
                self.gym.add_lines(self.viewer, self.envs[i], 1, [p0[0], p0[1], p0[2], pz[0], pz[1], pz[2]], [0, 0, 1])
        # Camera Debug
        # camera_tensor = self.gym.get_camera_image_gpu_tensor(self.sim, self.envs[0], self.camera_handles[0], gymapi.IMAGE_COLOR)
        # torch_camera_tensor = gymtorch.wrap_tensor(camera_tensor)
        # cam_img = torch_camera_tensor.cpu().numpy()
        # cam_img = Im.fromarray(cam_img)
        # plt.imshow(cam_img)
        # plt.pause(1e-9)
        # self.gym.end_access_image_tensors(self.sim)
        # self.debug_fig.clf()


#####################################################################
###=========================jit functions=========================###
#####################################################################


@torch.jit.script
def compute_baxter_reward(
    reset_buf, progress_buf, actions, cabinet_dof_pos,
    baxter_grasp_pos, drawer_grasp_pos, baxter_grasp_rot, drawer_grasp_rot,
    baxter_lfinger_pos, baxter_rfinger_pos, goal_buf,
    gripper_forward_axis, drawer_inward_axis, gripper_up_axis, drawer_up_axis,
    num_envs, dist_reward_scale, rot_reward_scale, around_handle_reward_scale, open_reward_scale,
    finger_dist_reward_scale, action_penalty_scale, distX_offset, max_episode_length
):
    # type: (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, int, float, float, float, float, float, float, float, float) -> Tuple[Tensor, Tensor]
    rewards = torch.zeros_like(reset_buf)
    rewards = torch.where(torch.abs(baxter_grasp_pos - goal_buf[:, :3]).mean(keepdim=1) < 0.02 and torch.abs(cabinet_dof_pos[: ,3] - goal_buf[: ,3]) < 0.01, rewards + 1, rewards)

    rewards = torch.where(torch.abs(baxter_grasp_pos - goal_buf[:, :3]).mean(keepdim=1) < 0.02 and torch.abs(cabinet_dof_pos[: ,3] - goal_buf[: ,3]) < 0.01, rewards + 1, rewards)

    reset_buf = torch.where(progress_buf >= max_episode_length - 1, torch.ones_like(reset_buf), reset_buf)
    return rewards, reset_buf


@torch.jit.script
def compute_grasp_transforms(hand_rot, hand_pos, baxter_local_grasp_rot, baxter_local_grasp_pos,
                             drawer_rot, drawer_pos, drawer_local_grasp_rot, drawer_local_grasp_pos
                             ):
    # type: (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor) -> Tuple[Tensor, Tensor, Tensor, Tensor]

    global_baxter_rot, global_baxter_pos = tf_combine(
        hand_rot, hand_pos, baxter_local_grasp_rot, baxter_local_grasp_pos)
    global_drawer_rot, global_drawer_pos = tf_combine(
        drawer_rot, drawer_pos, drawer_local_grasp_rot, drawer_local_grasp_pos)

    return global_baxter_rot, global_baxter_pos, global_drawer_rot, global_drawer_pos
