from datetime import datetime
import os
import time

from gym.spaces import Space

import numpy as np
import statistics
from collections import deque

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

from utils.rl_pytorch.sac import ReplayBeffer


class SAC:

    def __init__(self,
                 vec_env,
                 actor_critic_class,
                 num_learning_epochs,
                 demonstration_buffer_len = 13650,
                 replay_buffer_len = 1000000,
                 gamma=0.99,
                 init_noise_std=1.0,
                 learning_rate=1e-3,
                 tau = 0.005,
                 alpha = 0.2,
                 reward_scale = 16,
                 batch_size = 256,
                 schedule="fixed",
                 desired_kl=None,
                 model_cfg=None,
                 device='cuda:0',
                 log_dir='run',
                 is_testing=False,
                 print_log=True,
                 apply_reset=False,
                 asymmetric=False
                 ):

        if not isinstance(vec_env.observation_space, Space):
            raise TypeError("vec_env.observation_space must be a gym Space")
        if not isinstance(vec_env.state_space, Space):
            raise TypeError("vec_env.state_space must be a gym Space")
        if not isinstance(vec_env.action_space, Space):
            raise TypeError("vec_env.action_space must be a gym Space")
        self.observation_space = vec_env.observation_space
        self.action_space = vec_env.action_space
        self.state_space = vec_env.state_space

        self.device = device
        self.asymmetric = asymmetric

        self.desired_kl = desired_kl
        self.schedule = schedule
        self.step_size = learning_rate
        self.is_testing = is_testing
        self.current_learning_iteration = 0
        self.num_learning_epochs = num_learning_epochs
        self.demonstration_buffer_len = demonstration_buffer_len
        self.replay_buffer_len = replay_buffer_len

        # Log
        self.log_dir = log_dir
        self.print_log = print_log
        self.writer = SummaryWriter(log_dir=self.log_dir, flush_secs=10)
        self.tot_timesteps = 0
        self.tot_time = 0

        # SAC components
        self.vec_env = vec_env
        self.actor_critic = actor_critic_class(self.observation_space.shape, self.state_space.shape, self.action_space.shape,
                                               init_noise_std, model_cfg, asymmetric=asymmetric)
        self.actor_critic.to(self.device)

        # Initialize the optimizer
        q_lr = learning_rate
        value_lr = learning_rate
        policy_lr = learning_rate
        self.reward_scale = reward_scale
        self.value_optimizer = optim.Adam(self.actor_critic.value_net.parameters(), lr=value_lr)
        self.q1_optimizer = optim.Adam(self.actor_critic.q1_net.parameters(), lr=q_lr)
        self.q2_optimizer = optim.Adam(self.actor_critic.q2_net.parameters(), lr=q_lr)
        self.policy_optimizer = optim.Adam(self.actor_critic.policy_net.parameters(), lr=policy_lr)

        self.buffer = ReplayBeffer(self.replay_buffer_len, self.demonstration_buffer_len)
        # hyperparameters
        self.gamma = gamma
        self.tau = tau
        self.alpha = alpha
        self.batch_size = batch_size

        # Load the target value network parameters
        for target_param, param in zip(self.actor_critic.target_value_net.parameters(), self.actor_critic.value_net.parameters()):
            target_param.data.copy_(self.tau * param + (1 - self.tau) * target_param)

        # SAC parameters
        self.state_dim = self.vec_env.observation_space.shape[0]
        self.action_dim = self.vec_env.action_space.shape[0]

        self.apply_reset = apply_reset

    def test(self, path):
        self.actor_critic.load_state_dict(torch.load(path))
        self.actor_critic.eval()

    def load(self, path):
        self.actor_critic.load_state_dict(torch.load(path))
        self.current_learning_iteration = int(path.split("_")[-1].split(".")[0])
        self.actor_critic.train()

    def save(self, path):
        torch.save(self.actor_critic.state_dict(), path)

    def run(self, num_learning_iterations, log_interval=1):
        current_obs = self.vec_env.reset()
        current_states = self.vec_env.get_state()

        if self.is_testing:
            while True:
                with torch.no_grad():
                    if self.apply_reset:
                        current_obs = self.vec_env.reset()
                    # Compute the action
                    actions = self.actor_critic.act_inference(current_obs)
                    # Step the vec_environment
                    next_obs, rews, dones, infos = self.vec_env.step(actions)
                    current_obs.copy_(next_obs)
        else:
            Return = []
            action_range = torch.Tensor([self.action_space.low, self.action_space.high]).to('cuda:0')
            states = current_obs

            for it in range(self.current_learning_iteration, num_learning_iterations):
                score = 0
                current_obs = self.vec_env.reset()
                # Rollout
                for _ in range(500):
                    if self.apply_reset:
                        current_obs = self.vec_env.reset()
                        current_states = self.vec_env.get_state()
                    # Compute the action
                    if self.buffer.buffer_len() >= self.demonstration_buffer_len:
                        actions = self.actor_critic.act(states)
                    else:
                        actions = self.vec_env.get_reverse_actions()
                        # actions = self.actor_critic.act(states)
                    # action_in =  actions * (action_range[1] - action_range[0]) / 2.0 + (action_range[1] + action_range[0]) / 2.0
                    # Step the vec_environment
                    next_states, reward, done, _ = self.vec_env.step(actions)
                    # implement reward scale
                    reward *= self.reward_scale

                    if self.buffer.buffer_len() < self.demonstration_buffer_len:
                        self.buffer.push_demonstration_data((states, actions, reward, next_states, done), 50)
                    else:
                        self.buffer.push((states, actions, reward, next_states, done))
                    states = next_states

                    score += reward
                    # if done:
                    #     break
                    if self.buffer.buffer_len() >= self.demonstration_buffer_len + 1:
                        self.update(self.batch_size)

                print("episode:{}, score:{}, buffer_capacity:{}".format(it, score.mean(), self.buffer.buffer_len()))
                self.writer.add_scalar('Reward/Reward', score.mean(), it)
                Return.append(score)
                score = 0
                
                if it % log_interval == 0:
                    self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(it)))
            self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(num_learning_iterations)))

    def update(self, batch_size):
        
        state, action, reward, next_state, done = self.buffer.sample(batch_size)
        # new_action, log_prob = self.actor_critic.evaluate(state)
        
        # # V value loss
        # value = self.actor_critic.value_net(state)
        # new_q1_value = self.actor_critic.q1_net(state, new_action)
        # new_q2_value = self.actor_critic.q2_net(state, new_action)
        # next_value = torch.min(new_q1_value, new_q2_value) - self.alpha * log_prob
        # value_loss = F.mse_loss(value, next_value.detach())
        # # Soft Q loss
        # q1_value = self.actor_critic.q1_net(state, action)
        # q2_value = self.actor_critic.q2_net(state, action)
        # target_value = self.actor_critic.target_value_net(next_state)
        # target_q_value = reward + self.gamma * target_value
        # # target_q_value = reward + self.gamma * target_value

        # q1_value_loss = F.mse_loss(q1_value, target_q_value.detach())
        # q2_value_loss = F.mse_loss(q2_value, target_q_value.detach())

        # # Policy loss
        # policy_loss = (self.alpha * log_prob - torch.min(new_q1_value, new_q2_value)).mean()

        # # Update Policy
        # self.policy_optimizer.zero_grad()
        # policy_loss.backward()
        # self.policy_optimizer.step()

        # # Update v
        # self.value_optimizer.zero_grad()
        # value_loss.backward()
        # self.value_optimizer.step()

        # # Update Soft q
        # self.q1_optimizer.zero_grad()
        # self.q2_optimizer.zero_grad()
        # q1_value_loss.backward()
        # q2_value_loss.backward()
        # self.q1_optimizer.step()
        # self.q2_optimizer.step()

        # # Update target networks
        # for target_param, param in zip(self.actor_critic.target_value_net.parameters(), self.actor_critic.value_net.parameters()):
        #     target_param.data.copy_(self.tau * param + (1 - self.tau) * target_param)


        # Set up function for computing Q function
        q1_value = self.actor_critic.q1_net(state, action)
        q2_value = self.actor_critic.q2_net(state, action)
        action2, log_prob2 = self.actor_critic.evaluate(next_state)
        target_q1_value = self.actor_critic.target_q1_net(next_state, action2)
        target_q2_value = self.actor_critic.target_q2_net(next_state, action2)
        backup = reward + self.gamma * (torch.min(target_q1_value, target_q2_value) - self.alpha * log_prob2)

        q1_value_loss = ((q1_value - backup) ** 2).mean()
        q2_value_loss = ((q2_value - backup) ** 2).mean()

        # Update Soft q
        self.q1_optimizer.zero_grad()
        self.q2_optimizer.zero_grad()
        q1_value_loss.backward(retain_graph=True)
        q2_value_loss.backward()
        self.q1_optimizer.step()
        self.q2_optimizer.step()

        # Set up function for computing SAC pi loss
        new_action, log_prob = self.actor_critic.evaluate(state)

        q1_pi_value = self.actor_critic.q1_net(state, new_action)
        q2_pi_value = self.actor_critic.q2_net(state, new_action)

        # Policy loss
        policy_loss = (self.alpha * log_prob - torch.min(q1_pi_value, q2_pi_value)).mean()

        # Update Policy
        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()

        # Update target networks
        for target_param, param in zip(self.actor_critic.target_q1_net.parameters(), self.actor_critic.q1_net.parameters()):
            target_param.data.copy_(self.tau * param + (1 - self.tau) * target_param)
        for target_param, param in zip(self.actor_critic.target_q2_net.parameters(), self.actor_critic.q2_net.parameters()):
            target_param.data.copy_(self.tau * param + (1 - self.tau) * target_param)



