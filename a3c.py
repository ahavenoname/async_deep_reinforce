# -*- coding: utf-8 -*-
import tensorflow as tf
import threading
import numpy as np

import signal
import random
import math
import os
import time

from game_ac_network import GameACFFNetwork, GameACLSTMNetwork
from a3c_training_thread import A3CTrainingThread
from rmsprop_applier import RMSPropApplier

import options
options = options.options

def log_uniform(lo, hi, rate):
  log_lo = math.log(lo)
  log_hi = math.log(hi)
  v = log_lo * (1-rate) + log_hi * rate
  return math.exp(v)

device = "/cpu:0"
if options.use_gpu:
  device = "/gpu:0"

initial_learning_rate = log_uniform(options.initial_alpha_low,
                                    options.initial_alpha_high,
                                    options.initial_alpha_log_rate)

global_t = 0

stop_requested = False

if options.use_lstm:
  global_network = GameACLSTMNetwork(options.action_size, -1, device)
else:
  global_network = GameACFFNetwork(options.action_size, device)


training_threads = []

learning_rate_input = tf.placeholder("float")

grad_applier = RMSPropApplier(learning_rate = learning_rate_input,
                              decay = options.rmsp_alpha,
                              momentum = 0.0,
                              epsilon = options.rmsp_epsilon,
                              clip_norm = options.grad_norm_clip,
                              device = device)

for i in range(options.parallel_size):
  training_thread = A3CTrainingThread(i, global_network, initial_learning_rate,
                                      learning_rate_input,
                                      grad_applier, options.max_time_step,
                                      device = device, options = options)
  training_threads.append(training_thread)

# prepare session
sess = tf.Session(config=tf.ConfigProto(log_device_placement=False,
                                        allow_soft_placement=True))

init = tf.initialize_all_variables()
sess.run(init)

# summary for tensorboard
score_input = tf.placeholder(tf.int32)
tf.scalar_summary("score", score_input)

summary_op = tf.merge_all_summaries()
summary_writer = tf.train.SummaryWriter(options.log_file, sess.graph_def)

# init or load checkpoint with saver
saver = tf.train.Saver(max_to_keep = options.max_to_keep)
checkpoint = tf.train.get_checkpoint_state(options.checkpoint_dir)
if checkpoint and checkpoint.model_checkpoint_path:
  saver.restore(sess, checkpoint.model_checkpoint_path)
  print("checkpoint loaded:", checkpoint.model_checkpoint_path)
  tokens = checkpoint.model_checkpoint_path.split("-")
  # set global step
  global_t = int(tokens[1])
  print(">>> global step set: ", global_t)
  # set wall time
  wall_t_fname = options.checkpoint_dir + '/' + 'wall_t.' + str(global_t)
  with open(wall_t_fname, 'r') as f:
    wall_t = float(f.read())
  next_save_steps = (global_t + options.save_time_interval)//options.save_time_interval * options.save_time_interval
else:
  print("Could not find old checkpoint")
  # set wall time
  wall_t = 0.0
  next_save_steps = options.save_time_interval


def save_data():
  if not os.path.exists(options.checkpoint_dir):
    os.mkdir(options.checkpoint_dir)  

  # need copy of global_t because it might be changed in other thread
  global_t_copy = global_t

  # write wall time
  wall_t = time.time() - start_time
  wall_t_fname = options.checkpoint_dir + '/' + 'wall_t.' + str(global_t_copy)
  with open(wall_t_fname, 'w') as f:
    f.write(str(wall_t))

  saver.save(sess, options.checkpoint_dir + '/' + 'checkpoint', global_step = global_t_copy)

  print('@@@ Data saved at global_t={}'.format(global_t_copy))


def train_function(parallel_index):
  global global_t
  global next_save_steps
  
  training_thread = training_threads[parallel_index]
  # set start_time
  start_time = time.time() - wall_t
  training_thread.set_start_time(start_time)

  while True:
    if (parallel_index == 0) and (global_t > next_save_steps):
      save_data()
      next_save_steps += options.save_time_interval
      
    if stop_requested:
      break
    if global_t > options.save_time_step:
      break

    diff_global_t = training_thread.process(sess, global_t, summary_writer,
                                            summary_op, score_input)
    global_t += diff_global_t
    
    
def signal_handler(signal, frame):
  global stop_requested
  print('You pressed Ctrl+C!')
  stop_requested = True
  
train_threads = []
for i in range(options.parallel_size):
  train_threads.append(threading.Thread(target=train_function, args=(i,)))
  
signal.signal(signal.SIGINT, signal_handler)

# set start time
start_time = time.time() - wall_t

for t in train_threads:
  t.start()

print('Press Ctrl+C to stop')

for t in train_threads:
  t.join()

save_data()
