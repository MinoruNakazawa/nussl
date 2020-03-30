# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:light
#     text_representation:
#       extension: .py
#       format_name: light
#       format_version: '1.5'
#       jupytext_version: 1.4.1
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# Training deep models in *nussl*
# ==============================
#
# *nussl* has tight integration with the entire deep learning pipeline for computer audition, 
# with a focus on source separation. This pipeline includes using and building new 
# source separation architectures, handling data, training the architectures via
# an easy to use API powered by [PyTorch Ignite](https://pytorch.org/ignite/index.html),
# evaluating model performance, and using the models on new audio signals. Trained
# models are also distributed via the [External File Zoo](http://nussl.ci.northwestern.edu/).
#
# This tutorial will walk you through *nussl*'s model training capabilities on a simple
# synthetic dataset for illustration purposes. While *nussl* has support for a broad variety of models, 
# we will focus on straight-forward mask inference networks.

# SeparationModel
# ---------------
#
# At the heart of *nussl*'s deep learning pipeline is the SeparationModel class.
# SeparationModel takes in a description of the model architecture and instantiates it.
# Model architectures are described via a dictionary. A model architecture has three
# parts: the building blocks, or *modules*, how the building blocks are wired together,
# and the outputs of the model.
#
# ### Modules ##
#
# Let's take a look how a simple architecture is described. This model will be a single
# linear layer that estiamtes the spectra for 3 sources for every frame in the STFT.

# +
import nussl
import json
import torch
import numpy as np
import matplotlib.pyplot as plt
import copy
import tempfile
import os
import logging
from concurrent.futures import ThreadPoolExecutor
import shutil
import termtables
import glob
import tqdm

# seed this notebook
nussl.utils.seed(0)

# define the building blocks
num_features = 129 # number of frequencies in STFT
num_sources = 3 # how many sources to estimate
mask_activation = 'sigmoid' # activation function for masks
num_audio_channels = 1 # number of audio channels

modules = {
    'mix_magnitude': {},
    'log_spectrogram': {
        'class': 'AmplitudeToDB'
    },
    'normalization': {
        'class': 'BatchNorm',
    },
    'mask': {
        'class': 'Embedding',
        'args': {
            'num_features': num_features,
            'hidden_size': num_features,
            'embedding_size': num_sources,
            'activation': mask_activation,
            'num_audio_channels': num_audio_channels,
            'dim_to_embed': [2, 3] # embed the frequency dimension (2) for all audio channels (3)
        }
    },
    'estimates': {
        'class': 'Mask',
    },
}


# -

# The lines above define the building blocks, or *modules* of the SeparationModel. 
# There are four building blocks: `mix_magnitude`, `log_spectrogram`, `normalization`,
# and `mask`. 
#
# Each module in the dictionary has a key and a
# value. The key tells SeparationModel the name of that layer in the architecture. 
# For example, `log_spectrogram` will be the name of a building block. The value is
# also a dictionary with two values: `class` and `args`. `class` tells SeparationModel
# what the code for this module should be. `args` tells SeparationModel what the 
# arguments to the class should be when instantiating it. Finally, if the dictionary
# that the key points to is empty, then it is assumed to be something that comes from
# the input dictionary to the model.
#
# So where does the code for each of these classes live? The code for these modules
# is in `nussl.ml.modules`. The existing modules in *nussl* are as follows:

# +
def print_existing_modules():
    excluded = ['checkpoint', 'librosa', 'nn', 'np', 'torch', 'warnings']
    print('nussl.ml.modules contents:')
    existing_modules = [x for x in dir(nussl.ml.modules) if x not in excluded and not x.startswith('__')]
    print('\n'.join(existing_modules))

print_existing_modules()
# -

# Descriptions of each of these modules and their arguments can be found in the API docs.
# In the model we have described above, we have used: 
#
# 1. `AmplitudeToDB` to extract log-magnitude spectrograms from the input `mix_magnitude`.
# 2. `BatchNorm` to normalize each spectrogram input by the mean and standard
#    deviation of all the data (one mean/std for the entire spectrogram, not per feature).
# 3. `Embedding` to embed each 129-dimensional frame into 3*129-dimensional space with a
#    sigmoid activation.
# 4. `Mask` to take the output of the embedding and element-wise multiply it by the input
#    `mix_magnitude` to generate source estimates.
#    
# ### Connections ###
#
# Now we have to define the next part of SeparationModel - how the modules are wired together.
# We do this by defining the `connections` of the model.

# define the topology
connections = [
    ['log_spectrogram', ['mix_magnitude', ]],
    ['normalization', ['log_spectrogram', ]],
    ['mask', ['normalization', ]],
    ['estimates', ['mask', 'mix_magnitude']]
]

# `connections` is a list of lists. Each item of `connections` has two elements. The first
# element contains the name of the module (defined in `modules`). The second element
# contains the arguments that will go into the module defined in the first element.
#
# So for example, `log_spectrogram`, which corresponded to the `AmplitudeToDB`
# class takes in `mix_magnitude`. In the forward pass `mix_magnitude` corresponds to
# the data in the input dictionary. The output of `log_spectrogram` (which is a 
# log-magnitude spectrogram) is passed to the module named `normalization`, which
# is a `BatchNorm` layer. This output is then passed to the `mask` module, which
# constructs the masks using an `Embedding` class. Finally, the source estimates
# are constructed by passing both `mix_magnitude` and `mask` to the `estimates`
# module, which uses a `Mask` class.
#
# Complex forward passes can be defined via these connections. Connections can be
# even more detailed. Modules can take in keyword arguments by making the second
# element a dictionary. If modules also output a dictionary, then specific outputs
# can be reference in the connections via `module_name:key_in_dictionary`. For
# example, `nussl.ml.modules.GaussianMixtureTorch` (which is a differentiable
# GMM unfolded on some input data) outputs a dictionary with
# the following keys: `resp, log_prob, means, covariance, prior`. If this module
# was named `gmm`, then these outputs can be used in the second element via
# `gmm:means`, `gmm:resp`, `gmm:covariance`, etc.
#
# ### Output and forward pass ###
#
# Next, models have to actually output some data to be used later on. Let's have
# this model output the `estimates` and the `mask` data by doing this:

# define the outputs
output = ['estimates', 'mask']

# You can use these outputs directly or you can use them as a part of a 
# larger deep learning pipeline. SeparationModel can be, for example, a
# first step before you do something more complicated with the output
# that doesn't fit cleanly into how SeparationModels are built.
#
# Finally, let's put it all together in one config dictionary. The dictionary
# must have the following keys to be valid: `modules`, `connections`, and 
# `output`. If these keys don't exist, then SeparationModel will throw
# an error.

# +
# put it together
config = {
    'modules': modules,
    'connections': connections,
    'output': output
}

print(json.dumps(config, indent=2))
# -

# Let's load this config into SeparationModel and print the model
# architecture:

model = nussl.ml.SeparationModel(config)
print(model)

# Now let's put some random data through it, with the expected size.

# batch size is 1, 400 frames, 129 frequencies, and 1 audio channel
mix_magnitude = torch.rand(1, 400, 129, 1)
model(mix_magnitude)

# Uh oh! Putting in the data directly resulted in an error. This is because 
# SeparationModel expects a *dictionary*. The dictionary must contain all of the
# input keys that were defined. Here it was `mix_magnitude`. So let's try 
# again:

mix_magnitude = torch.rand(1, 400, 129, 1)
data = {'mix_magnitude': mix_magnitude}
output = model(data)

# Now we have passed the data through the model. Note a few things here:
#
# 1. The tensor passed through the model had the following shape:
#    `(n_batch, sequence_length, num_frequencies, num_audio_channels)`. This is
#    different from how STFTs for an AudioSignal are shaped. Those are shaped as:
#    `(num_frequencies, sequence_length, num_audio_channels`. We added a batch
#    dimension here, and the ordering of frequency and audio channel dimensions
#    were swapped. This is because recurrent networks are a popular way to process
#    spectrograms, and these expect (and operate more efficiently) when sequence
#    length is right after the batch dimension.
# 2. The key in the dictionary had to match what we put in the configuration
#    before.
# 3. We embedded *both* the channel dimension (3) as well as the frequency dimension (2)
#    when building up the configuration.
#
# Now let's take a look at what's in the output!

output.keys()

# There are two keys as expected: `estimates` and `mask`. They both have the
# same shape as `mix_magnitude` with one addition:

output['estimates'].shape, output['mask'].shape

# The last dimension is 3! Which is the number of sources we're trying to
# separate. Let's look at the first source.

# +
i = 0
plt.figure(figsize=(5, 5))
plt.imshow(output['estimates'][0, ..., 0, i].T.cpu().data.numpy())
plt.title("Source")
plt.show()

plt.figure(figsize=(5, 5))
plt.imshow(output['mask'][0, ..., 0, i].T.cpu().data.numpy())
plt.title("Mask")
plt.show()
# -

# ### Saving and loading a model ###
#
# Now let's save this model and load it back up.

with tempfile.NamedTemporaryFile(suffix='.pth', delete=True) as f:
    loc = model.save(f.name)
    reloaded_dict = torch.load(f.name)
    
    print(reloaded_dict.keys())
    
    new_model = nussl.ml.SeparationModel(reloaded_dict['config'])
    new_model.load_state_dict(reloaded_dict['state_dict'])
    
    print(new_model)


# When models are saved, both the config AND the weights are saved. Both of these can be easily
# loaded back into a new SeparationModel object.

# ### Custom modules ###
#
# There's also straightforward support for *custom* modules that don't 
# exist in *nussl* but rather exist in the end-user code. These can be
# registered with SeparationModel easily. Let's build a custom module
# and register it with a copy of our existing model. Let's make this 
# module a LambdaLayer, which takes in some arbitrary function and runs 
# it on the input.

# +
class Lambda(torch.nn.Module):
    def __init__(self, func):
        self.func = func
        super().__init__()
    
    def forward(self, data):
        return self.func(data)

def print_shape(x):
    print(f'Shape is {x.shape}')

lamb = Lambda(print_shape)
output = lamb(mix_magnitude)
# -

# Now let's put it into a copy of our model and update the connections so that it
# prints for every layer.

# +
new_connections = [
    ['log_spectrogram', ['mix_magnitude', ]],
    ['lambda', ['mix_magnitude', ]],
    ['lambda', ['log_spectrogram', ]],
    ['normalization', ['log_spectrogram', ]],
    ['lambda', ['normalization', ]],
    ['mask', ['normalization', ]],
    ['lambda', ['mask', ]],
    ['estimates', ['mask', 'mix_magnitude']],
    ['lambda', ['estimates', ]]
]

new_modules = copy.deepcopy(modules)
new_modules['lambda'] = {
    'class': 'Lambda',
    'args': {
        'func': print_shape
    }
}
new_config = {
    'modules': new_modules,
    'connections': new_connections,
    'output': ['estimates', 'mask']
}
# -

# But right now, SeparationModel doesn't know about our Lambda class! So,
# let's make it aware by registering the module with nussl:

nussl.ml.register_module(Lambda)
print_existing_modules()

# Now Lambda is a registered module! Let's build the SeparationModel and put some data through
# it:

verbose_model = nussl.ml.SeparationModel(new_config)
output = verbose_model(data)


# We can see the outputs of the Lambda layer recurring after each connection. Note that because we used a 
# non-serializable argument to the LambdaLayer, this model won't save without special handling! So,
# be careful.

# Alright, now let's get some actual audio data for this thing...

# Handling data
# -------------
#
# As described in the datasets tutorial, the heart of *nussl* data handling
# is BaseDataset and its associated subclasses. We built a simple one in that
# tutorial that just produced random sine waves. Let's grab it again:

# +
def make_sine_wave(freq, sample_rate, duration):
    dt = 1 / sample_rate
    x = np.arange(0.0, duration, dt)
    x = np.sin(2 * np.pi * freq * x)
    return x

class SineWaves(nussl.datasets.BaseDataset):
    def get_items(self, folder):
        # ignore folder and return a list
        # 100 items in this dataset
        items = list(range(100))
        return items
    
    def process_item(self, item):
        # we're ignoring items and making
        # sums of random sine waves
        sources = {}
        freqs = []
        for i in range(3):
            freq = np.random.randint(110, 1000)
            freqs.append(freq)
            _data = make_sine_wave(freq, self.sample_rate, 2)
            # this is a helper function in BaseDataset for
            # making an audio signal from data
            signal = self._load_audio_from_array(_data)
            signal.path_to_input_file = f'{item}.wav'
            sources[f'sine{i}'] = signal * 1/3
        
        mix = sum(sources.values())
        
        metadata = {
            'frequencies': freqs
        }
        
        output = {
            'mix': mix,
            'sources': sources,
            'metadata': metadata
        }
        return output


# -

# As a reminder, this dataset makes random mixtures of sine waves with fundamental frequencies
# between 110 Hz and 1000 Hz. Let's now set it up with appropriate STFT parameters that result
# in 129 frequencies in the spectrogram.

# +
folder = 'ignored'
stft_params = nussl.STFTParams(window_length=256, hop_length=64)
sine_wave_dataset = SineWaves(
    folder, sample_rate=8000, stft_params=stft_params)

item = sine_wave_dataset[0]

def visualize_and_embed(sources, y_axis='mel'):
    plt.figure(figsize=(10, 4))
    plt.subplot(111)
    nussl.utils.visualize_sources_as_masks(
        sources, db_cutoff=-60, y_axis=y_axis)
    plt.tight_layout()
    plt.show()

    nussl.play_utils.multitrack(sources, ext='.wav')
    
visualize_and_embed(item['sources'])
# -

# Let's check the shape of the `mix` stft:

item['mix'].stft().shape

# Great! There's 129 frequencies and 251 frames and 1 audio channel. To put it into our
# model though, we need the STFT in the right shape, and we also need some training data.
# Let's use some of *nussl*'s transforms to do this. Specifically, we'll use the
# `PhaseSensitiveSpectrumApproximation` and the `ToSeparationModel` transforms. We'll 
# also use the `MagnitudeWeights` transform in case we want to use deep clustering loss
# functions.

folder = 'ignored'
stft_params = nussl.STFTParams(window_length=256, hop_length=64)
tfm = nussl.datasets.transforms.Compose([
    nussl.datasets.transforms.PhaseSensitiveSpectrumApproximation(),
    nussl.datasets.transforms.MagnitudeWeights(),
    nussl.datasets.transforms.ToSeparationModel()
])
sine_wave_dataset = SineWaves(
    folder, sample_rate=8000, stft_params=stft_params,
    transform=tfm)
item = sine_wave_dataset[0]
item.keys()

# Now the item has all the keys that SeparationModel needs. It's a dictionary with exactly
# what we need! And the `ToSeparationModel` transform swapped the frequency and sequence length
# dimension appropriately, and made them all torch Tensors:

item['mix_magnitude'].shape

# We still need to add a batch dimension and make everything have float type
# though. So let's do that for each key, if the key is a torch Tensor:

# +
for key in item:
    if torch.is_tensor(item[key]):
        item[key] = item[key].unsqueeze(0).float()
        
item['mix_magnitude'].shape
# -

# Now we can pass this through our model:

# +
output = model(item)

i = 0
plt.figure(figsize=(5, 5))
plt.imshow(
    output['estimates'][0, ..., 0, i].T.cpu().data.numpy(),
    origin='lower')
plt.title("Source")
plt.show()

plt.figure(figsize=(5, 5))
plt.imshow(
    output['mask'][0, ..., 0, i].T.cpu().data.numpy(),
    origin='lower')
plt.title("Mask")
plt.show()


# -

# We've now seen how to use *nussl* transforms, datasets, and SeparationModel
# together to make a forward pass. Now, let's see how to train the model so
# it actually does something.

# Closures and loss functions
# ---------------------------
#
# *nussl* trains models via *closures*, which define the forward and backward passes for a
# model on a single batch. Closures use *loss functions* within them, which compute the 
# loss on a single batch. There are a bunch of common loss functions already in *nussl*.

# +
def print_existing_losses():
    excluded = ['nn', 'torch', 'combinations', 'permutations']
    print('nussl.ml.train.loss contents:')
    existing_losses = [x for x in dir(nussl.ml.train.loss) if x not in excluded and not x.startswith('__')]
    print('\n'.join(existing_losses))

print_existing_losses()
# -

# In addition to standard loss functions for spectrograms, like L1 Loss and MSE, there is also
# an SDR loss for time series audio, as well as permutation invariant versions of
# these losses for training things like speaker separation networks. A closure uses these
# loss functions in a simple way. For example, here is the code for training a model
# with a closure:

# +
from nussl.ml.train.closures import Closure
from nussl.ml.train import BackwardsEvents

class TrainClosure(Closure):
    """
    This closure takes an optimization step on a SeparationModel object given a
    loss.
    
    Args:
        loss_dictionary (dict): Dictionary containing loss functions and specification.
        optimizer (torch Optimizer): Optimizer to use to train the model.
        model (SeparationModel): The model to be trained.
    """

    def __init__(self, loss_dictionary, optimizer, model):
        super().__init__(loss_dictionary)
        self.optimizer = optimizer
        self.model = model

    def __call__(self, engine, data):
        self.model.train()
        self.optimizer.zero_grad()

        output = self.model(data)

        loss_ = self.compute_loss(output, data)
        loss_['loss'].backward()
        engine.fire_event(BackwardsEvents.BACKWARDS_COMPLETED)
        self.optimizer.step()
        loss_ = {key: loss_[key].item() for key in loss_}

        return loss_


# -

# So, this closure takes some data and puts it through the model, then calls
# `self.compute_loss` on the result, fires an event on the ignite `engine`, 
# and then steps the optimizer on the loss. This is a standard PyTorch training
# loop. The magic here is happening in `self.compute_loss`, which comes from the
# parent class `Closure`.

# ### Loss dictionary ###
#
# The parent class `Closure` takes a loss dictionary which defines the losses that get 
# computed on the output of the model. The loss dictionary has the following format:
#
#     loss_dictionary = {
#             'LossClassName': {
#                 'weight': [how much to weight the loss in the sum, defaults to 1],
#                 'keys': [key mapping items in dictionary to arguments to loss],
#                 'args': [any positional arguments to the loss class],
#                 'kwargs': [keyword arguments to the loss class],
#             }
#         }
#         
# For example, one possible loss could be:

loss_dictionary = {
    'DeepClusteringLoss': {
        'weight': .2,
    },
    'PermutationInvariantLoss': {
        'weight': .8,
        'args': ['L1Loss']
    }
}

# This will apply the deep clustering and a permutation invariant L1 loss to the output
# of the model. So, how does the model know what to compare? Each loss function is a 
# class in *nussl*, and each class has an attribute called `DEFAULT_KEYS`, This attribute
# tells the Closure how to use the forward pass of the loss function. For example, this is
# the code for the L1 Loss:

# +
from torch import nn

class L1Loss(nn.L1Loss):
    DEFAULT_KEYS = {'estimates': 'input', 'source_magnitudes': 'target'}


# -

# [L1Loss](https://pytorch.org/docs/stable/nn.html?highlight=l1%20loss#torch.nn.L1Loss) 
# is defined in PyTorch and has the following example for its forward pass:
#
#     >>> loss = nn.L1Loss()
#     >>> input = torch.randn(3, 5, requires_grad=True)
#     >>> target = torch.randn(3, 5)
#     >>> output = loss(input, target)
#     >>> output.backward()
#     
# The arguments to the function are `input` and `target`. So the mapping from the dictionary
# provided by our dataset and model jointly is to use `estimates` as the input and 
# `source_magnitudes` (what we are trying to match) as the target. This results in 
# the `DEFAULT_KEYS` you see above. Alternatively, you can pass the mapping between
# the dictionary and the arguments to the loss function directly into the loss dictionary
# like so:

loss_dictionary = {
    'L1Loss': {
        'weight': 1.0,
        'keys': {
            'estimates': 'input',
            'source_magnitudes': 'target',
        }
    }
}

# Great, now let's use this loss dictionary in a Closure and see what happens.

closure = nussl.ml.train.closures.Closure(loss_dictionary)
closure.losses

# The closure was instantiated with the losses. Calling `closure.compute_loss` results
# in the following:

output = model(item)
loss_output = closure.compute_loss(output, item)
for key, val in loss_output.items():
    print(key, val)


# The output is a dictionary with the `loss` item corresponding to the total
# (summed) loss and the other keys corresponding to the individual losses.

# ### Custom loss functions ###
#
# Loss functions can be registered with the Closure in the same way that
# modules are registered with SeparationModel:

# +
class MeanDifference(torch.nn.Module):
    DEFAULT_KEYS = {'estimates': 'input', 'source_magnitudes': 'target'}
    def __init__(self):
        super().__init__()
    
    def forward(self, input, target):
        return torch.abs(input.mean() - target.mean())

nussl.ml.register_loss(MeanDifference)
print_existing_losses()
# -

# Now this loss can be used in a closure:

# +
new_loss_dictionary = {
    'MeanDifference': {}
}

new_closure = nussl.ml.train.closures.Closure(new_loss_dictionary)
new_closure.losses

output = model(item)
loss_output = new_closure.compute_loss(output, item)
for key, val in loss_output.items():
    print(key, val)
# -

# ### Optimizing the model ###
#
# We now have a loss. We can then put it backwards through the model and
# take a step forward on the model with an optimizer. Let's define
# an optimizer (we'll use Adam), and then use it to take a step on
# the model:

# +
optimizer = torch.optim.Adam(model.parameters(), lr=.001)

optimizer.zero_grad()
output = model(item)
loss_output = closure.compute_loss(output, item)
loss_output['loss'].backward()
optimizer.step()
# -

# Cool, we did a single step. Instead of manually defining this all above, we can 
# instead use the TrainClosure from *nussl*.

# ?nussl.ml.train.closures.TrainClosure

train_closure = nussl.ml.train.closures.TrainClosure(
    loss_dictionary, optimizer, model
)

# The `__call__` function of the closure takes an `engine` as well as the batch data. 
# Since we don't currently have an `engine` object, let's just pass `None`.
# We can run this on a batch:

train_closure(None, item)

# We can run this a bunch of times and watch the loss go down.

# +
loss_history = []
n_iter = 100

for i in range(n_iter):
    loss_output = train_closure(None, item)
    loss_history.append(loss_output['loss'])
# -

plt.plot(loss_history)
plt.title('Train loss')
plt.xlabel('Iteration')
plt.ylabel('Loss')
plt.show()

# Note that there is also a `ValidationClosure` which does not take
# an optimization step but only computes the loss. 
#
# Let's look at the model output now!

# +
output = model(item)

for i in range(output['estimates'].shape[-1]):
    plt.figure(figsize=(10, 5))
    plt.subplot(121)
    plt.imshow(
        output['estimates'][0, ..., 0, i].T.cpu().data.numpy(),
        origin='lower')
    plt.title("Source")

    plt.subplot(122)
    plt.imshow(
        output['mask'][0, ..., 0, i].T.cpu().data.numpy(),
        origin='lower')
    plt.title("Mask")
    plt.show()
# -

# We've now overfit the model to a single item in the dataset. Now, let's do it
# at scale by using ignite Engines with the functionality in `nussl.ml.train`.

# Engines
# -------
#
# *nussl* uses PyTorch Ignite to power its training functionality. PyTorch
# At the heart of Ingite is the *Engine* object. An Engine contains a lot
# of functionality for iterating through a dataset and feeding data to a model.
# The only thing a user has to define in Ignite is a *closure* which defines
# a pass through the model for a single batch. The rest of the details, such
# as queueing up data, are taken care of by `torch.utils.data.DataLoader` and
# the engine object. All of the state regarding a training run, such as the
# epoch number, the loss history, etc, is kept in the engine's state at
# `engine.state`.
#
# To get a standard engine that has functionality like keeping track of 
# loss history, and prepares the batches properly, and sets up the 
# train and validation closures, the function `create_train_and_validation_engines`
# is provided.
#
# Handlers that add functionality can be attached to a Engine objects. These
# handlers make use of the engine's state. *nussl* comes with several of these:
#
# 1. `add_validate_and_checkpoint`: Adds a pass on the validation data and 
#     checkpoints the model based on the validation loss to either `best`
#     (if this was the lowest validation loss model) or `latest`.
# 2. `add_stdout_handler`: Prints some handy information after each epoch.
# 3. `add_tensorboard_handler`: Logs loss data to tensorboard.
#
# ### Putting it all together ###
#
# Let's put this all together. Let's build the dataset, model and
# optimizer, train and validation closures, and engines.

# +
BATCH_SIZE = 5
LEARNING_RATE = 1e-3
OUTPUT_FOLDER = os.path.expanduser('~/.nussl/tutorial/sinewave')
RESULTS_DIR = os.path.join(OUTPUT_FOLDER, 'results')
NUM_WORKERS = 2

shutil.rmtree(os.path.join(RESULTS_DIR), ignore_errors=True)

os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# adjust logging so we see output of the handlers
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Put together data
stft_params = nussl.STFTParams(window_length=256, hop_length=64)
tfm = nussl.datasets.transforms.Compose([
    nussl.datasets.transforms.PhaseSensitiveSpectrumApproximation(),
    nussl.datasets.transforms.MagnitudeWeights(),
    nussl.datasets.transforms.ToSeparationModel()
])
sine_wave_dataset = SineWaves(
    'ignored', sample_rate=8000, stft_params=stft_params,
    transform=tfm)
dataloader = torch.utils.data.DataLoader(
    sine_wave_dataset, batch_size=BATCH_SIZE)

# Build our simple model
model = nussl.ml.SeparationModel(config)

# Build an optimizer
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)

# Set up loss functions and closure
# We'll use permutation invariant loss since we don't
# care what order the sine waves get output in, just that
# they are different.
loss_dictionary = {
    'PermutationInvariantLoss': {
        'weight': 1.0,
        'args': ['L1Loss']
    }
}

train_closure = nussl.ml.train.closures.TrainClosure(
    loss_dictionary, optimizer, model)
val_closure = nussl.ml.train.closures.ValidationClosure(
    loss_dictionary, model)

# Build the engine and add handlers
train_engine, val_engine = nussl.ml.train.create_train_and_validation_engines(
    train_closure, val_closure)
nussl.ml.train.add_validate_and_checkpoint(
    OUTPUT_FOLDER, model, optimizer, sine_wave_dataset, train_engine,
    val_data=dataloader, validator=val_engine)
nussl.ml.train.add_stdout_handler(train_engine, val_engine)
# -

# Cool, we built an engine. Note the distinction between using the original dataset
# object and using the dataloader object. Now to train it, all we have to do is `run`
# the engine. Since our dataset as we made it is random sine waves,
# there is no concept of an epoch (e.g. one run through all of the data), we
# can instead define `epoch_length` to `train_engine`. After one epoch, 
# the validation will be run and everything will get printed by the
# `stdout` handler. Let's see:

train_engine.run(dataloader, epoch_length=1000)

# We can check out the loss over each iteration in the single epoch
# by examining the state:

plt.plot(train_engine.state.iter_history['loss'])
plt.xlabel('Iteration')
plt.ylabel('Loss')
plt.title('Train Loss')
plt.show()

# Let's also see what got saved in the output folder:

# !tree {OUTPUT_FOLDER}

# So the models and optimizers got saved! Let's load back one of these
# models and see what's in it.

# What's in a model?
# ------------------

saved_model = torch.load(train_engine.state.saved_model_path)
print(saved_model.keys())

# So there's as expected before the `state_dict` containing the weights of
# the trained model, the `config` containing the configuration of the model.
# There also a `metadata` key in the saved model. Let's see what's in there...

print(saved_model['metadata'].keys())

# There's a whole bunch of stuff related to training, like the folder 
# it was trained on, the state dictionary of the engine used to train the 
# model, the loss history for each epoch (not each iteration - that's too big).
#
# There are also keys that are related to the parameters of the AudioSignal. 
# Namely, `stft_params`, `sample_rate`, and `num_channels`. These 
# are used by *nussl* to prepare an AudioSignal object to be put into a
# deep learning based separation algorithm. There's also a `transforms`
# key - this is used by *nussl* to construct the input dictionary at
# inference time on an AudioSignal so that the data going into the model
# matches how it was given during training time. Let's look at each of these:

for key in saved_model['metadata']:
    print(f"{key}: {saved_model['metadata'][key]}")


# Cool! Alright, let's move on to actually using and evaluating the trained
# model.

# Using and evaluating a trained model
# ------------------------------------
#
# In this tutorial, we built a deep mask estimation network. There is a 
# corresponding separation algorithm in *nussl* for using 
# deep mask estimation networks. Let's build our dataset
# again, this time *without* transforms, so we have access to
# the actual AudioSignal objects. Then let's instantiate the
# separation algorithm and use it to separate an item from the 
# dataset.

# +
sine_wave_dataset = SineWaves(
    'ignored', sample_rate=8000)
item = sine_wave_dataset[0]
MODEL_PATH = os.path.join(OUTPUT_FOLDER, 'checkpoints/best.model.pth')

separator = nussl.separation.deep.DeepMaskEstimation(
    item['mix'], model_path = MODEL_PATH
)
estimates = separator()

visualize_and_embed(estimates)
# -

# ### Evaluation in parallel ###
#
# We'll usually want to run many mixtures through the model, separate,
# and get evaluation metrics like SDR, SIR, and SAR. We can do that with
# the following bit of code:

# +
# make a separator with an empty audio signal initially
# this one will live on gpu (if one exists) and be used in a 
# threadpool for speed
dme = nussl.separation.deep.DeepMaskEstimation(
    nussl.AudioSignal(), model_path=MODEL_PATH, device='cuda')

def forward_on_gpu(audio_signal):
    # set the audio signal of the object to this item's mix
    dme.audio_signal = audio_signal
    masks = dme.forward()
    return masks

def separate_and_evaluate(item, masks):
    separator = nussl.separation.deep.DeepMaskEstimation(item['mix'])
    estimates = separator(masks)

    evaluator = nussl.evaluation.BSSEvalScale(
        list(item['sources'].values()), estimates, compute_permutation=True)
    scores = evaluator.evaluate()
    output_path = os.path.join(
        RESULTS_DIR, f"{item['mix'].file_name}.json")
    with open(output_path, 'w') as f:
        json.dump(scores, f)

pool = ThreadPoolExecutor(max_workers=NUM_WORKERS)
for i, item in enumerate(tqdm.tqdm(sine_wave_dataset)):
    masks = forward_on_gpu(item['mix'])
    if i == 0:
        separate_and_evaluate(item, masks)
    else:
        pool.submit(separate_and_evaluate, item, masks)
pool.shutdown(wait=True)

json_files = glob.glob(f"{RESULTS_DIR}/*.json")
df = nussl.evaluation.aggregate_score_files(json_files)

overall = df.mean()
headers = ["", f"OVERALL (N = {df.shape[0]})", ""]
metrics = ["SAR", "SDR", "SIR"]
data = np.array(df.mean()).T

data = [metrics, data]
termtables.print(data, header=headers, padding=(0, 1), alignment="ccc")
# -

# We parallelized the evaluation across 2 workers, kept two copies of
# the separator, one of which lives on the GPU, and the other which
# lives on the CPU. The GPU one does a forward pass in its own thread
# and then hands it to the other separator which actually computes the
# estimates and evaluates the metrics in parallel. After we're done, 
# we aggregate all the results (each of which was saved to a JSON file)
# using `nussl.evaluation.aggregate_score_files` and use a nice package
# called `termtables` to print the output. We also now have the results 
# as a pandas DataFrame:

df

# Finally, we can look at the structure of the output folder again,
# seeing there are now 100 entries under results corresponding to each
# item in `sine_wave_dataset`:

# !tree --filelimit 20 {OUTPUT_FOLDER}
