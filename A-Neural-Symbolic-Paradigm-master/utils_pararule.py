"""Data utils for logic-memnn."""
import json
import socket
import numpy as np
import json_lines
import re

import keras.callbacks as C
from keras.utils import Sequence
from keras.preprocessing.sequence import pad_sequences

from data_gen import CHAR_IDX
from word_dict_gen import WORD_INDEX
import os
import random

class LogicSeq(Sequence):
  """Sequence generator for normal logic programs."""
  def __init__(self, datasets, batch_size, train=True,
               shuffle=True, pad=False, zeropad=True):
    self.datasets = datasets or [[]]
    # We distribute batch evenly so it must divide the batc size
    assert batch_size % len(self.datasets) == 0, "Number of datasets must divide batch size."
    self.batch_size = batch_size
    self.train = train
    self.shuffle = shuffle
    self.pad = pad
    self.zeropad = zeropad
    seed_value = 0
    os.environ['PYTHONHASHSEED'] = str(seed_value)
    random.seed(seed_value)
    np.random.seed(seed_value)

  def __len__(self):
    return int(np.ceil(sum(map(len, self.datasets))/ self.batch_size))

  def on_epoch_end(self):
    """Shuffle data at the end of epoch."""
    if self.shuffle:
      for ds in self.datasets:
        np.random.shuffle(ds)

  def __getitem__(self, idx):
    dpoints = list()
    per_ds_bs = self.batch_size//len(self.datasets)
    for ds in self.datasets:
      dpoints.extend(ds[idx*per_ds_bs:(idx+1)*per_ds_bs])
    # Create batch
    ctxs, queries, targets = list(), list(), list()
    for ctx, q, t in dpoints:
      if self.shuffle:
        np.random.shuffle(ctx)
      # rules = [r.replace(':-', '.').replace(';', '.').split('.')[:-1]
      #          for r in ctx]
      rules = []
      for r in ctx:
        result = []
        result.append(r)
        rules.append(result)
      # if self.pad:
      #   rules.append(['()']) # Append blank rule
      # if self.zeropad:  pred.split(" ")   q.split(" ")   filter_data(re.split(r"[\s]", q))
      #   rules.append(['']) # Append null sentinel filter_data(re.split(r"[\s]", pred))

      rules = [[[WORD_INDEX[c] for c in filter_data(re.split(r"[\s]", pred))]
                for pred in r]
               for r in rules]
      ctxs.append(rules)
      queries.append([WORD_INDEX[c] for c in filter_data(re.split(r"[\s]", q))]) # Remove '.' at the end
      targets.append(t)
    vctxs = np.zeros((len(dpoints),
                      max([len(rs) for rs in ctxs]),
                      max([len(ps) for rs in ctxs for ps in rs]),
                      max([len(cs) for rs in ctxs for ps in rs for cs in ps])),
                     dtype='int')
    # Contexts
    for i in range(len(dpoints)):
      # Rules in context (ie program)
      for j in range(len(ctxs[i])):
        # Predicates in rules
        for k in range(len(ctxs[i][j])):
          # Chars in predicates
          for l in range(len(ctxs[i][j][k])):
            vctxs[i, j, k, l] = ctxs[i][j][k][l]
    xs = [vctxs, pad_sequences(queries, padding='post')]
    if self.train:
      return xs, np.array(targets)
    return xs

  @staticmethod
  def parse_file(fname, shuffle=True):
    """Parse logic program data given fname."""
    dpoints = list()
    with open(fname) as f:
      for l in json_lines.reader(f):
        ctx = list()
        questions = l["questions"]
        context = l["context"].replace("\n", " ")
        context = context.replace(",", "")
        context = context.replace("!", "")
        context = re.sub(r'\s+', ' ', context)
        context = context.lower()
        for i in range(len(questions)):
            text = questions[i]["text"]
            label = questions[i]["label"]
            if label == True:
              t = 1
            else:
              t = 0
            q = re.sub(r'\s+', ' ', text)
            q = q.replace('.','')
            q = q.replace('!', '')
            q = q.replace(',', '')
            #ctx = context.split(".")
            ctx = filter_data(re.split(r"[.]", context))
            q = q.lower()
            #ctx = re.split(r"([.])", context)
            #ctx = ["".join(i) for i in zip(ctx[0::2], ctx[1::2])]
            dpoints.append((ctx, q, int(t)))
    if shuffle:
      np.random.shuffle(dpoints)
    return dpoints

  @classmethod
  def from_file(cls, fname, batch_size, pad=False, verbose=True):
    """Load logic programs from given fname."""
    dpoints = cls.parse_file(fname)
    if verbose:
      print("Example data points from:", fname)
      print(dpoints[:4])
    return cls([dpoints], batch_size, pad=pad)

  @classmethod
  def from_files(cls, fnames, batch_size, pad=False, verbose=True):
    """Load several logic program files return a singel sequence generator."""
    datasets = [cls.parse_file(f) for f in fnames]
    if verbose:
      print("Loaded files:", fnames)
    return cls(datasets, batch_size, pad=pad)


class ThresholdStop(C.Callback):
  """Stop when monitored value is greater than threshold."""
  def __init__(self, monitor='val_acc', threshold=1):
    super().__init__()
    self.monitor = monitor
    self.threshold = threshold

  def on_epoch_end(self, epoch, logs=None):
    current = logs.get(self.monitor)
    if current >= self.threshold:
      self.model.stop_training = True


class StatefulCheckpoint(C.ModelCheckpoint):
  """Save extra checkpoint data to resume training."""
  def __init__(self, weight_file, state_file=None, **kwargs):
    """Save the state (epoch etc.) along side weights."""
    super().__init__(weight_file, **kwargs)
    self.state_f = state_file
    self.hostname = socket.gethostname()
    self.state = dict()
    if self.state_f:
      # Load the last state if any
      try:
        with open(self.state_f, 'r') as f:
          self.state = json.load(f)
        self.best = self.state['best']
      except Exception as e: # pylint: disable=broad-except
        print("Skipping last state:", e)

  def on_train_begin(self, logs=None):
    prefix = "Resuming" if self.state else "Starting"
    print("{} training on {}".format(prefix, self.hostname))

  def on_epoch_end(self, epoch, logs=None):
    """Saves training state as well as weights."""
    super().on_epoch_end(epoch, logs)
    if self.state_f:
      state = {'epoch': epoch+1, 'best': self.best,
               'hostname': self.hostname}
      state.update(logs)
      state.update(self.params)
      with open(self.state_f, 'w') as f:
        json.dump(state, f)

  def get_last_epoch(self, initial_epoch=0):
    """Return last saved epoch if any, or return default argument."""
    return self.state.get('epoch', initial_epoch)

  def on_train_end(self, logs=None):
    print("Training ending on {}".format(self.hostname))


# ����2�����зֺ󣬹��˵�split���ص�list�еĿ��ַ���
# filter_data()�����Ĺ����ǣ�����һ����string��ɵ�list [str1, str2, str3, ......]�����˵���Щ���ַ���''�������ַ���'\n'�������ع��˺����list
def not_break(sen):
  return (sen != '\n' and sen != '\u3000' and sen != '' and not sen.isspace())

def filter_data(ini_data):
  # ini_data���ɾ�����ɵ�string
  new_data = list(filter(not_break, [data.strip() for data in ini_data]))
  return new_data