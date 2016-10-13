# -*- coding: utf-8 -*-
import sys
import numpy as np
import cv2
import os
import math

import options
options = options.options
if options.use_gym:
  import gym
  from gym.envs.atari.atari_env import AtariEnv
  from atari_py import ALEInterface
else:
  from ale_python_interface import ALEInterface


class GameState(object):
  def __init__(self, rand_seed, options, display=False, no_op_max=30, thread_index=-1):
    if options.use_gym:
      self._display = options.display
    else:
      self.ale = ALEInterface()
      self.ale.setInt(b'random_seed', rand_seed)
      self.ale.setFloat(b'repeat_action_probability', options.repeat_action_probability)
      self.ale.setInt(b'frame_skip', options.frames_skip_in_ale)
      self.ale.setBool(b'color_averaging', options.color_averaging_in_ale)
    self._no_op_max = no_op_max
 
    self.options = options
    self.color_maximizing = options.color_maximizing_in_gs
    self.color_averaging  = options.color_averaging_in_gs
    self.color_no_change  = options.color_no_change_in_gs
    # for screen output in _process_frame()
    self.thread_index = thread_index
    self.record_gs_screen_dir = self.options.record_gs_screen_dir
    self.episode_record_dir = None
    self.episode = 1
    self.rooms = np.zeros((24), dtype=np.int)
    self.prev_room_no = 1
    self.room_no = 1
    self.new_room = -1

    if options.use_gym:
      # see https://github.com/openai/gym/issues/349
      def _seed(self, seed=None):
        self.ale.setFloat(b'repeat_action_probability', options.repeat_action_probability)
        from gym.utils import seeding
        self.np_random, seed1 = seeding.np_random(seed)
        # Derive a random seed. This gets passed as a uint, but gets
        # checked as an int elsewhere, so we need to keep it below
        # 2**31.
        seed2 = seeding.hash_seed(seed1 + 1) % 2 ** 31
        # Empirically, we need to seed before loading the ROM.
        self.ale.setInt(b'random_seed', seed2)
        self.ale.loadROM(self.game_path)
        return [seed1, seed2]
      
      AtariEnv._seed = _seed
      self.gym = gym.make(options.gym_env)
      self.ale = self.gym.ale
      print(self.gym.action_space)
    else:
      if display:
        self._setup_display()
    
      self.ale.loadROM(options.rom.encode('ascii'))

      # collect minimal action set
      self.real_actions = self.ale.getMinimalActionSet()
      print("real_actions=", self.real_actions)
      if (len(self.real_actions) != self.options.action_size):
        print("***********************************************************")
        print("* action_size != len(real_actions)")
        print("***********************************************************")
        sys.exit(1)

    # height=210, width=160
    self._screen = np.empty((210 * 160 * 1), dtype=np.uint8)
    if (not options.use_gym) and (self.color_maximizing or self.color_averaging or self.color_no_change):
      self._screen_RGB = np.empty((210 * 160 * 3), dtype=np.uint8)
      self._prev_screen_RGB = np.empty((210 *  160 * 3), dtype=np.uint8)
    self._have_prev_screen_RGB = False

    # for pseudo-count
    self.psc_use = options.psc_use
    if options.psc_use:
      self.psc_frsize = options.psc_frsize
      self.psc_k = options.psc_frsize ** 2
      self.psc_rev_pow = 1.0 / options.psc_pow
      self.psc_alpha = math.pow(0.1, options.psc_pow)
      self.psc_beta = options.psc_beta
      self.psc_maxval = options.psc_maxval
      self.psc_vcount = np.zeros((self.psc_k, self.psc_maxval + 1), dtype=np.float64)
      self.psc_n = 0

    self.reset()

  # for pseudo-count
  def psc_set_psc_info(self, psc_info):
    if psc_info["psc_n"] != 0:
      self.psc_vcount = np.array(psc_info["psc_vcount"], dtype=np.float32)
      self.psc_n = psc_info["psc_n"]
 
  # for pseudo-count
  def psc_add_image(self, psc_image):
    if psc_image.dtype != np.dtype('uint8'):
      print("Internal ERROR in dtype")
      sys.exit(1)
    k = self.psc_k
    n = self.psc_n
    if n > 0:
      nr = (n + 1.0)/n
      vcount = self.psc_vcount[range(k), psc_image]
      self.psc_vcount[range(k), psc_image] += 1.0
      r_over_rp = np.prod(nr * vcount / (1.0 + vcount))
      psc_count = r_over_rp / (1.0 - r_over_rp + 1.0e-37)
      psc_reward = self.psc_beta / math.pow(psc_count + self.psc_alpha, self.psc_rev_pow)
    else:
      self.psc_vcount[range(k), psc_image] += 1.0
      psc_reward = 0.0
    
    self.psc_n += 1

    if self.psc_n % (self.options.score_log_interval * 10) == 0:
      room = -1
      if self.options.rom == "montezuma_revenge.bin" or self.options.gym_env == "MontezumaRevenge-v0":
        ram = self.ale.getRAM()
        room = ram[3]
      print("[PSC]th={},psc_n={}:room={},psc_reward={:.8f},RM{:02d}".format(self.thread_index, self.psc_n, room, psc_reward, self.room_no))

    return psc_reward   

  # for montezuma's revenge
  def update_montezuma_rooms(self):
    ram = self.ale.getRAM()
    # room_no = ram[0x83]
    room_no = ram[3]
    self.rooms[room_no] += 1
    if self.rooms[room_no] == 1:
      print("[PSC]@@@ NEW ROOM({}) VISITED: visit counts={}".format(room_no, self.rooms))
      self.new_room = room_no
    self.prev_room_no = self.room_no
    self.room_no = room_no

  def set_record_screen_dir(self, record_screen_dir):
    if options.use_gym:
      print("record_screen_dir", record_screen_dir)
      self.gym.monitor.start(record_screen_dir)
      self.reset()
    else:
      print("record_screen_dir", record_screen_dir)
      self.ale.setString(b'record_screen_dir', str.encode(record_screen_dir))
      self.ale.loadROM(self.options.rom.encode('ascii'))
      self.reset()

  def close_record_screen_dir(self):
    if options.use_gym:
      self.gym.monitor.close()
    else:
      pass

  def _process_action(self, action):
    if options.use_gym:
      observation, reward, terminal, _ = self.gym.step(action)
      return reward, terminal
    else:
      reward = self.ale.act(action)
      terminal = self.ale.game_over()
      self.terminal = terminal
      self._have_prev_screen_RGB = False
      return reward, terminal
    
  def _process_frame(self, action, reshape):
    if self.terminal:
      reward = 0
      terminal = True
    elif options.use_gym:
      observation, reward, terminal, _ = self.gym.step(action)
      self._screen_RGB = observation
      self.terminal = terminal
    else:
      # get previous screen
      if (self.color_maximizing or self.color_averaging) \
              and not self._have_prev_screen_RGB:
        self.ale.getScreenRGB(self._prev_screen_RGB)
        self._have_prev_screen_RGB = True

      # make action
      reward = self.ale.act(action)
      terminal = self.ale.game_over()
      self.terminal = terminal

    # screen shape is (210, 160, 1)
    if self.color_maximizing or self.color_averaging: # impossible in gym
      self.ale.getScreenRGB(self._screen_RGB)
      if self._have_prev_screen_RGB:
        if self.color_maximizing:
          screen = np.maximum(self._prev_screen_RGB, self._screen_RGB)
        else: # self.color_averaging:
          screen = np.mean((self._prev_screen_RGB, self._screen_RGB), axis=0).astype(np.uint8)
      else:
        screen = self._screen_RGB
      screen = screen.reshape((210, 160, 3))
      self._screen = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY)
      # swap screen_RGB
      swap_screen_RGB = self._prev_screen_RGB
      self._prev_screen_RGB = self._screen_RGB
      self._screen_RGB = swap_screen_RGB
      self._have_prev_screen_RGB = True
    elif self.color_no_change:
      if not options.use_gym:
        self.ale.getScreenRGB(self._screen_RGB)
      screen = self._screen_RGB
      screen = screen.reshape((210, 160, 3))
      self._screen = cv2.cvtColor(screen, cv2.COLOR_RGB2GRAY)
    else:
      self.ale.getScreenGrayscale(self._screen)
    
    # reshape it into (210, 160)
    reshaped_screen = np.reshape(self._screen, (210, 160))
    
    # resize to height=110, width=84
    if self.options.crop_frame:
      resized_screen = cv2.resize(reshaped_screen, (84, 110))
      x_t = resized_screen[18:102,:]
    else:
      x_t = cv2.resize(reshaped_screen, (84, 84))
    x_t_uint8 = x_t
    
    if reshape:
      x_t = np.reshape(x_t, (84, 84, 1))
    x_t = x_t.astype(np.float32)
    x_t *= (1.0/255.0)
    return reward, terminal, x_t, x_t_uint8

  def pseudo_count(self, x_t):
    psc_reward = 0.0
    if self.psc_use:
      psc_image = cv2.resize(x_t, (self.psc_frsize, self.psc_frsize))
      psc_image = np.reshape(psc_image, (self.psc_k))
      psc_image = np.uint8(psc_image * (self.psc_maxval / 255.0))
      psc_reward = self.psc_add_image(psc_image)

    # update covered rooms
    if self.options.rom == "montezuma_revenge.bin" or self.options.gym_env == "MontezumaRevenge-v0":
      self.update_montezuma_rooms()
    
    return psc_reward
    
  def _setup_display(self):
    if sys.platform == 'darwin':
      import pygame
      pygame.init()
      self.ale.setBool(b'sound', False)
    elif sys.platform.startswith('linux'):
      self.ale.setBool(b'sound', True)
    self.ale.setBool(b'display_screen', True)

  def reset(self):
    if options.use_gym:
      self.gym.reset()
    else:
      self.ale.reset_game()
    
    # randomize initial state
    if self._no_op_max > 0:
      no_op = np.random.randint(0, self._no_op_max // self.options.frames_skip_in_ale + 1)
      if options.use_gym:
        no_op = no_op // 3 # gym skip 2 - 4 frame randomly
      for _ in range(no_op):
        if options.use_gym:
          self.gym.step(0)
        else:
          self.ale.act(0)

    self._have_prev_screen_RGB = False
    self.terminal = False
    _, _, x_t, x_t_uint8 = self._process_frame(0, False)
    _ = self.pseudo_count(x_t_uint8)
    
    self.reward = 0
    self.s_t = np.stack((x_t, x_t, x_t, x_t), axis = 2)

    self.lives = float(self.ale.lives())
    self.initial_lives = self.lives

    if (self.thread_index == 0) and (self.record_gs_screen_dir is not None):
      episode_dir = "episode{:03d}".format(self.episode)
      self.episode_record_dir = os.path.join(self.record_gs_screen_dir, episode_dir)
      os.makedirs(self.episode_record_dir)
      self.episode += 1
      self.stepNo = 1
      print("game_state: writing screen images to ", self.episode_record_dir)

    self.new_room = -1
    
  def process(self, action):
    if options.use_gym:
      real_action = action
      if self._display:
        self.gym.render()
    else:
      # convert original 18 action index to minimal action set index
      real_action = self.real_actions[action]
    reward = 0

    if self.options.stack_frames_in_gs:
      s_t1 = []
      terminal = False
      for _ in range(self.options.frames_skip_in_gs):
        if not terminal:
          r, t, x_t1, x_t_uint8 = self._process_frame(real_action, False)
          reward = reward + r
          terminal = terminal or t
        s_t1.append(x_t1)
      self.s_t1 = np.stack(s_t1, axis = 2)
      # for _ in range(self.options.frames_skip_in_gs):
      #   r, t, x_t1, x_t_uint8 = self._process_frame(real_action, True)
      #   reward = reward + r
      #   self.s_t1 = np.append(self.s_t[:,:,1:], x_t1, axis = 2)
      #   if t:
      #     break
    else:
      # altered for speed up (reduce getScreen and color_maximizing)
      for _ in range(self.options.frames_skip_in_gs - 1):
        r, t = self._process_action(real_action)
        reward = reward + r
        if t:
          self.terminal = True
          break

      r, t, x_t1, x_t_uint8 = self._process_frame(real_action, True)
      reward = reward + r
      self.s_t1 = np.append(self.s_t[:,:,1:], x_t1, axis = 2)

    self.reward = reward
    self.terminal = t

    self.psc_reward = self.pseudo_count(x_t_uint8)
    self.lives = float(self.ale.lives())

    if self.episode_record_dir is not None:
      filename = "{:06d}.png".format(self.stepNo)
      filename = os.path.join(self.episode_record_dir, filename)
      self.stepNo += 1
      screen_image = x_t1.reshape((84, 84)) * 255.
      cv2.imwrite(filename, screen_image)


  def update(self):
    self.s_t = self.s_t1
