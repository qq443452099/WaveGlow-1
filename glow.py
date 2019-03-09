import tensorflow as tf
import numpy as np
from params import hparams


def create_variable(name, shape):
    '''Create a convolution filter variable with the specified name and shape,
    and initialize it using Xavier initialition.'''
    initializer = tf.contrib.layers.xavier_initializer_conv2d()
    variable = tf.Variable(initializer(shape=shape), name=name)
    return variable


def create_bias_variable(name, shape):
    '''Create a bias variable with the specified name and shape and initialize
    it to zero.'''
    initializer = tf.constant_initializer(value=0.0, dtype=tf.float32)
    return tf.Variable(initializer(shape=shape), name)


def create_variable_zeros(name, shape):
    '''Create a convolution filter variable with the specified name and shape,
    and initialize it using Xavier initialition.'''
    initializer = tf.constant_initializer(0.0)
    variable = tf.Variable(initializer(shape=shape), name=name)
    return variable


def compute_waveglow_loss(z, log_s_list, log_det_W_list, sigma=1.0):
    '''negative log-likelihood of the data x'''
    for i, log_s in enumerate(log_s_list):
        if i == 0:
            log_s_total = tf.reduce_sum(log_s)
            log_det_W_total = log_det_W_list[i]
        else:
            log_s_total = log_s_total + tf.reduce_sum(log_s)
            log_det_W_total += log_det_W_list[i]

    loss = tf.reduce_sum(z * z) / (2 * sigma * sigma) - log_s_total - log_s_total
    shape = tf.shape(z)
    loss = loss / (shape[0] * shape[1] * shape[2])
    return loss


def invertible1x1Conv(z, n_channels, forward=True, name='inv1x1conv'):
    with tf.variable_scope(name):
        shape = tf.shape(z)
        batch_size, length, channels = shape[0], shape[1], shape[2]

        # sample a random orthogonal matrix to initialize weight
        W_init = np.linalg.qr(np.random.randn(n_channels, n_channels))
        W = tf.get_variable('W', initializer=W_init)

        # compute log determinant
        logdet = batch_size * length * tf.log(tf.abs(tf.matrix_determinant(W)))

        if forward:
            _W = tf.reshape(W, [1, n_channels, n_channels])
            z = tf.nn.conv1d(z, _W, stride=1, padding='SAME')
            return z, logdet
        else:
            _W = tf.matrix_inverse(W)
            _W = tf.reshape(_W, [1, n_channels, n_channels])
            z = tf.nn.conv1d(z, _W, stride=1, padding='SAME')
            return z


class WaveNet(object):
    def __init__(self, n_in_channels, n_lc_dim, n_layers,
                 residual_channels=512, skip_channels=256, kernel_size=3, name='wavenet'):
        self.n_in_channels = n_in_channels
        self.n_lc_dim = n_lc_dim  # 80 * 8
        self.n_layers = n_layers
        self.residual_channels = residual_channels
        self.skip_channels = skip_channels
        self.kernel_size = kernel_size
        self.name = name

    def create_network(self, audio_batch, lc_batch):
        with tf.variable_scope(self.name):
            # channel convert
            w_s = create_variable('w_s', [1, self.n_in_channels, self.residual_channels])
            b_s = create_bias_variable('b_s', [self.residual_channels])
            audio_batch = tf.nn.bias_add(tf.nn.conv1d(audio_batch, w_s, 1, 'SAME'), b_s)

            skip_outputs = []
            for i in range(self.n_layers):
                dilation = 2 ** i
                audio_batch, _skip_output = self.dilated_conv1d(audio_batch, lc_batch, dilation)
                skip_outputs.append(_skip_output)

            # post process
            skip_output = sum(skip_outputs)
            # learn scale and shift
            w_e = create_variable_zeros('w_e', [1, self.skip_channels, self.n_in_channels * 2])
            b_e = create_bias_variable('b_e', [self.n_in_channels * 2])
            audio_batch = tf.nn.bias_add(tf.nn.conv1d(skip_output, w_e, 1, 'SAME'), b_e)
            return audio_batch[:, :, :self.n_in_channels], audio_batch[:, :, self.n_in_channels:]

    def dilated_conv1d(self, audio_batch, lc_batch, dilation=1):
        input = audio_batch
        with tf.variable_scope('dilation_%d' % (dilation,)):
            # compute gate & filter
            w_g_f = create_variable('w_g_f', [self.kernel_size, self.residual_channels, 2 * self.residual_channels])
            b_g_f = create_bias_variable('b_g_f', [2 * self.residual_channels])

            # convert conv1d to conv2d to leverage dilated conv
            shape = tf.shape(audio_batch)
            _w_g_f = tf.reshape(w_g_f, [1, self.kernel_size, self.residual_channels, 2 * self.residual_channels])
            audio_batch = tf.reshape(audio_batch, [shape[0], 1, shape[1], shape[2]])
            audio_batch = tf.nn.bias_add(tf.nn.conv2d(audio_batch,
                                                      _w_g_f,
                                                      strides=[1, 1, 1, 1],
                                                      padding='SAME',
                                                      dilations=[1, 1, dilation, 1],
                                                      name='dilated_conv')
                                         + b_g_f)

            # convert back to B*T*d data
            audio_batch = tf.reshape(audio_batch, [shape[0], shape[1], -1])

            # process local condition
            w_lc = create_variable('w_lc', [1, self.n_lc_dim, 2 * self.residual_channels])
            b_lc = create_bias_variable('b_lc', [2 * self.residual_channels])
            lc_batch = tf.nn.bias_add(tf.nn.conv1d(lc_batch, w_lc, 1, 'SAME'), b_lc)

            # gated conv
            in_act = audio_batch + lc_batch  # add local condtion
            filter = tf.nn.tanh(in_act[:, :, :self.residual_channels])
            gate = tf.nn.sigmoid(in_act[:, :, self.residual_channels:])
            acts = gate * filter

            # skip
            w_skip = create_variable('w_skip', [1, self.residual_channels, self.skip_channels])
            b_skip = create_bias_variable('b_skip', [self.skip_channels])
            skip_output = tf.nn.bias_add(tf.nn.conv1d(acts, w_skip, 1, 'SAME'), b_skip)

            # residual conv1d
            w_res = create_variable('w_res', [1, self.residual_channels, self.residual_channels])
            b_res = create_bias_variable('b_res', [self.residual_channels])
            res_output = tf.nn.bias_add(tf.nn.conv1d(acts, w_res, 1, 'SAME'), b_res)

            return res_output + input, skip_output


class WaveGlow(object):
    def __init__(self, lc_dim=80, n_flows=12, n_group=8, n_early_every=4, n_early_size=2):
        self.lc_dim = lc_dim
        self.n_flows = n_flows
        self.n_group = n_group
        self.n_early_every = n_early_every
        self.n_early_size = n_early_size
        self.n_remaining_channels = n_group

    def create_forward_network(self, audio_batch, lc_batch, name='Waveglow'):
        '''
        :param audio_batch: B*T*1
        :param lc_batch: B*T*80, upsampled by directly repeat or transposed conv
        :param name:
        :return:
        '''
        with tf.variable_scope(name):
            batch, length = tf.shape(audio_batch)[0], tf.shape(audio_batch)[1]

            # sequeeze
            audio_batch = tf.reshape(audio_batch, [batch, -1, self.n_group])  # B*T'*8
            lc_batch = tf.reshape(lc_batch, [batch, -1, self.lc_dim * self.n_group])  # B*T'*640

            output_audio = []
            log_s_list = []
            log_det_W_list = []

            for k in range(1, self.n_flows + 1):
                if k % self.n_early_every == 0 and k != self.n_flows:
                    output_audio.append(audio_batch[:, :, :self.n_early_size])
                    audio_batch = audio_batch[:, :, self.n_early_size:]
                    self.n_remaining_channels -= self.n_early_size  # update remaining channels

                with tf.variable_scope('glow_%d' % (k,)):
                    # invertiable 1X1 conv
                    audio_batch, log_det_w = invertible1x1Conv(audio_batch, self.n_remaining_channels)
                    log_det_W_list.append(log_det_w)

                    # affine coupling layer
                    n_half = self.n_remaining_channels / 2
                    audio_0, audio_1 = audio_batch[:, :, :n_half], audio_batch[:, :, n_half:]

                    wavenet = WaveNet(n_half, self.lc_dim * self.n_group, hparams.n_layers,
                                      hparams.residual_channels, hparams.skip_channels)
                    log_s, shift = wavenet.create_network(audio_0, lc_batch)
                    audio_1 = audio_1 * tf.exp(log_s) + shift
                    audio_batch = tf.concat([audio_0, audio_1], axis=-1)

                    log_s_list.append(log_s)

            output_audio.append(audio_batch)
            return tf.concat(output_audio, axis=-1), log_s_list, log_det_W_list

    def infer(self, lc_batch, sigma=1.0, name='Waveglow'):
        with tf.variable_scope(name):
            # compute the remaining channels
            remaining_channels = self.n_group
            for k in range(1, self.n_flows + 1):
                if k % self.n_early_every == 0 and k != self.n_flows:
                    remaining_channels = remaining_channels - self.n_early_size

            batch = tf.shape(lc_batch)[0]
            # need to make sure that length of lc_batch be multiple times of n_group
            pad = tf.shape(lc_batch)[1] + self.n_group - tf.shape(lc_batch)[1] % self.n_group
            lc_batch = tf.pad(lc_batch, [[0, 0], [0, pad], [0, 0]])
            lc_batch = tf.reshape(lc_batch, [batch, -1, self.lc_dim * self.n_group])

            shape = tf.shape(lc_batch)
            audio_batch = tf.random_normal([shape[0], shape[1], remaining_channels])
            audio_batch = audio_batch * sigma

            # backward inference
            for k in reversed(range(1, self.n_group + 1)):
                with tf.variable_scope('glow_%d' % (k,)):
                    # affine coupling layer
                    n_half = remaining_channels / 2
                    audio_0, audio_1 = audio_batch[:, :, :n_half], audio_batch[:, :, n_half:]
                    wavenet = WaveNet(n_half, self.lc_dim * self.n_group, hparams.n_layers,
                                      hparams.residual_channels, hparams.skip_channels)
                    log_s, shift = wavenet.create_network(audio_0, lc_batch)
                    audio_1 = audio_1 * tf.exp(log_s) + shift
                    audio_batch = tf.concat([audio_0, audio_1], axis=-1)

                    # inverse 1X1 conv
                    audio_batch = invertible1x1Conv(audio_batch, self.n_remaining_channels, forward=False)

                # early output
                if k % self.n_early_every == 0 and k != self.n_flows:
                    z = tf.random_normal([shape[0], shape[1], self.n_early_size])
                    z = z * sigma
                    remaining_channels += self.n_early_size

                    audio_batch = tf.concat([z, audio_batch], axis=-1)

            # reshape audio back to B*T*1
            audio_batch = tf.reshape(audio_batch, [shape[0], -1, 1])
            return audio_batch
