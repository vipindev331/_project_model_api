import tensorflow
from tensorflow import keras
from tensorflow.keras.layers import (
    BatchNormalization, SeparableConv2D, MaxPooling2D, Activation, Flatten, Dropout, Dense
)
from keras.layers.convolutional import MaxPooling2D, Convolution2D, AveragePooling2D
from keras.layers import Input, Dropout, Dense, Flatten, Activation
from keras.layers.normalization import BatchNormalization
from keras.layers.merge import concatenate
from keras import regularizers
from keras import initializers
from keras.models import Model
# Backend
from keras import backend as K
# Utils
from keras.utils.layer_utils import convert_all_kernels_in_model
from keras.utils.data_utils import get_file

class CopyChannels(keras.layers.Layer):
    """
    This layer copies channels from channel_start the number of channels given in channel_count.
    """
    def __init__(self,
                 channel_start=0,
                 channel_count=1,
                 **kwargs):
        self.channel_start=channel_start
        self.channel_count=channel_count
        super(CopyChannels, self).__init__(**kwargs)
        
    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], self.channel_count)
    
    def call(self, x):
        return x[:, :, :, self.channel_start:(self.channel_start+self.channel_count)]
        
    def get_config(self):
        config = {
            'channel_start': self.channel_start,
            'channel_count': self.channel_count
        }
        base_config = super(CopyChannels, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class Negate(keras.layers.Layer):
    """
    This layer negates (multiplies by -1) the input tensor.
    """
    def __init__(self, **kwargs):
        super(Negate, self).__init__(**kwargs)
        self.trainable = False

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], input_shape[3])
    
    def call(self, x):
        return -x

class ConcatNegation(keras.layers.Layer):        
    """
    This layer concatenates to the input its negation.
    """
    def __init__(self, **kwargs):
        super(ConcatNegation, self).__init__(**kwargs)
        self.trainable = False

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], input_shape[3]*2)
    
    def call(self, x):
        #return np.concatenate((x, -x), axis=3)
        return keras.layers.Concatenate(axis=3)([x, -x])

class InterleaveChannels(keras.layers.Layer):
    """
    This layer interleaves channels stepping according to the number passed as parameter.
    """
    def __init__(self,
                 step_size=2,
                 **kwargs):
        if step_size < 2:
            self.step_size=1
        else:
            self.step_size=step_size
        super(InterleaveChannels, self).__init__(**kwargs)

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], input_shape[3])

    def call(self, x):
        return keras.layers.Concatenate(axis=3)(
            [ x[:, :, :, shift_pos::self.step_size] for shift_pos in range(self.step_size) ]
        )
        # for self.step_size == 2, we would have:
        #  return keras.layers.Concatenate(axis=3)([
        #    x[:, :, :, 0::self.step_size],
        #    x[:, :, :, 1::self.step_size]
        #    ])

    def get_config(self):
        config = {
            'step_size': self.step_size
        }
        base_config = super(InterleaveChannels, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

class SumIntoHalfChannels(keras.layers.Layer):
    """
    This layer divedes channels into 2 halfs and then sums resulting in half of the input channels.
    """
    def __init__(self, **kwargs):
        super(SumIntoHalfChannels, self).__init__(**kwargs)
        self.trainable = False

    def compute_output_shape(self, input_shape):
        return (input_shape[0], input_shape[1], input_shape[2], input_shape[3] // 2)

    def call(self, x):
        outputchannels = x.shape[3] // 2
        return tensorflow.math.add(
            x=x[:, :, :, 0:outputchannels],
            y=x[:, :, :, outputchannels:outputchannels*2]
            )

def GlobalAverageMaxPooling2D(previous_layer,  name=None):
    """
    Adds both global Average and Max poolings. This layers is known to speed up training.
    """
    if name is None: name='global_pool'
    return keras.layers.Concatenate(axis=1)([
      keras.layers.GlobalAveragePooling2D(name=name+'_avg')(previous_layer),
      keras.layers.GlobalMaxPooling2D(name=name+'_max')(previous_layer)
    ])

def FitChannelCountTo(last_tensor, next_channel_count, has_interleaving=False, channel_axis=3):
    prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
    full_copies = next_channel_count // prev_layer_channel_count
    extra_channels = next_channel_count % prev_layer_channel_count
    output_copies = []
    for copy_cnt in range(full_copies):
        if copy_cnt == 0:
            output_copies.append( last_tensor )
        else:
            if has_interleaving:
                output_copies.append( InterleaveChannels(step_size=((copy_cnt+1) % prev_layer_channel_count))(last_tensor) )
            else:
                output_copies.append( last_tensor )
    if (extra_channels > 0):
        if has_interleaving:
            extra_tensor = InterleaveChannels(step_size=((full_copies+1) % prev_layer_channel_count))(last_tensor)
        else:
            extra_tensor = last_tensor
        output_copies.append( CopyChannels(0,extra_channels)(extra_tensor) )
    last_tensor = keras.layers.Concatenate(axis=channel_axis)( output_copies )
    return last_tensor

def EnforceEvenChannelCount(last_tensor, channel_axis=3):
    prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
    if (prev_layer_channel_count % 2 > 0):
        last_tensor = FitChannelCountTo(
            last_tensor,
            next_channel_count=prev_layer_channel_count+1,
            channel_axis=channel_axis)
    return last_tensor

def BinaryConvLayers(last_tensor, name, shape=(3, 3), conv_count=1, has_batch_norm=True, has_interleaving=False, activation='relu', channel_axis=3):
    last_tensor = EnforceEvenChannelCount(last_tensor)
    prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
    for conv_cnt in range(conv_count):
        input_tensor = last_tensor
        if has_interleaving:
            last_tensor_interleaved = InterleaveChannels(step_size=2, name=name+"_i_"+str(conv_cnt))(last_tensor)
        else:
            last_tensor_interleaved = last_tensor
        x1 = keras.layers.Conv2D(prev_layer_channel_count//2, shape, padding='same', activation=None, name=name+"_a_"+str(conv_cnt), groups=prev_layer_channel_count//2)(last_tensor)
        x2 = keras.layers.Conv2D(prev_layer_channel_count//2, shape, padding='same', activation=None, name=name+"_b_"+str(conv_cnt), groups=prev_layer_channel_count//2)(last_tensor_interleaved)
        last_tensor = keras.layers.Concatenate(axis=channel_axis, name=name+"_conc_"+str(conv_cnt))([x1,x2])
        if has_batch_norm: last_tensor = keras.layers.BatchNormalization(axis=channel_axis, name=name+"_batch_"+str(conv_cnt))(last_tensor)
        if activation is not None: last_tensor = keras.layers.Activation(activation=activation, name=name+"_act_"+str(conv_cnt))(last_tensor)
        last_tensor = keras.layers.add([input_tensor, last_tensor], name=name+'_add'+str(conv_cnt))
        if has_batch_norm: last_tensor = keras.layers.BatchNormalization(axis=channel_axis)(last_tensor)
    return last_tensor

def BinaryPointwiseConvLayers(last_tensor, name, conv_count=1, has_batch_norm=True, has_interleaving=False, activation='relu', channel_axis=3):
    return BinaryConvLayers(last_tensor, name, shape=(1, 1), conv_count=conv_count, has_batch_norm=has_batch_norm, has_interleaving=has_interleaving,  activation=activation, channel_axis=channel_axis)

def BinaryCompressionLayer(last_tensor, name, has_batch_norm=True, activation='relu', channel_axis=3):
    last_tensor = EnforceEvenChannelCount(last_tensor)
    prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
    last_tensor = keras.layers.Conv2D(prev_layer_channel_count//2, (1, 1), padding='same', activation=None, name=name+"_conv", groups=prev_layer_channel_count//2)(last_tensor)
    if has_batch_norm: last_tensor = keras.layers.BatchNormalization(axis=channel_axis, name=name+"_batch")(last_tensor)
    if activation is not None: last_tensor = keras.layers.Activation(activation=activation, name=name+"_act")(last_tensor)
    return last_tensor

def BinaryCompression(last_tensor, name, target_channel_count, has_batch_norm=True, activation='relu', channel_axis=3):
    prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
    cnt = 0
    while (prev_layer_channel_count >= target_channel_count * 2):
        last_tensor = BinaryCompressionLayer(last_tensor, name=name+'_'+str(cnt), has_batch_norm=has_batch_norm, activation=activation, channel_axis=channel_axis)
        prev_layer_channel_count = keras.backend.int_shape(last_tensor)[channel_axis]
        cnt = cnt + 1
    if prev_layer_channel_count > target_channel_count:
        last_tensor = FitChannelCountTo(last_tensor, next_channel_count=target_channel_count*2, channel_axis=channel_axis)
        last_tensor = BinaryCompressionLayer(last_tensor, name=name+'_'+str(cnt), has_batch_norm=has_batch_norm, activation=activation, channel_axis=channel_axis)
    return last_tensor
  
def GetClasses():
    """
    This function returns CAI layer classes.
    """
    return {
        'CopyChannels': CopyChannels,
        'Negate': Negate,
        'ConcatNegation': ConcatNegation,
        'InterleaveChannels': InterleaveChannels,
        'SumIntoHalfChannels': SumIntoHalfChannels
    }
