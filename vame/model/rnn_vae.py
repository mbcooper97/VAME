#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Variational Animal Motion Embedding 0.1 Toolbox
© K. Luxem & P. Bauer, Department of Cellular Neuroscience
Leibniz Institute for Neurobiology, Magdeburg, Germany

https://github.com/LINCellularNeuroscience/VAME
Licensed under GNU General Public License v3.0
"""

import torch
from torch import nn
import torch.utils.data as Data
from torch.autograd import Variable
from torch.optim.lr_scheduler import StepLR

import os
import numpy as np
from pathlib import Path

from vame.util.auxiliary import read_config
from vame.model.dataloader import SEQUENCE_DATASET

# make sure torch uses cuda for GPU computing
use_gpu = torch.cuda.is_available()
if use_gpu:
    print("Using CUDA")
    print('GPU active:',torch.cuda.is_available())
    print('GPU used:',torch.cuda.get_device_name(0))
else:
    torch.device("cpu")



""" MODEL """
class Encoder(nn.Module):
    def __init__(self, NUM_FEATURES, hidden_size_layer_1, hidden_size_layer_2, dropout_encoder):
        super(Encoder, self).__init__()

        self.input_size = NUM_FEATURES
        self.hidden_size = hidden_size_layer_1
        self.hidden_size_2 = hidden_size_layer_2
        self.n_layers  = 1
        self.dropout   = dropout_encoder

        self.rnn_1 = nn.GRU(input_size=self.input_size, hidden_size=self.hidden_size, num_layers=self.n_layers,
                            bias=True, batch_first=True, dropout=self.dropout, bidirectional=True)

        self.rnn_2 = nn.GRU(input_size=self.hidden_size*2, hidden_size=self.hidden_size_2, num_layers=self.n_layers,
                            bias=True, batch_first=True, dropout=self.dropout, bidirectional=True)

    def forward(self, inputs):
        outputs_1, hidden_1 = self.rnn_1(inputs)
        outputs_2, hidden_2 = self.rnn_2(outputs_1)

        h_n_1 = torch.cat((hidden_1[0,...], hidden_1[1,...]), 1)
        h_n_2 = torch.cat((hidden_2[0,...], hidden_2[1,...]), 1)

        h_n = torch.cat((h_n_1, h_n_2), 1)

        return h_n


class Lambda(nn.Module):
    """Lambda module converts output of encoder to latent vector

    :param hidden_size: hidden size of the encoder
    :param latent_length: latent vector length
    """
    def __init__(self,ZDIMS, hidden_size_layer_1, hidden_size_layer_2):
        super(Lambda, self).__init__()

        self.hid_dim = hidden_size_layer_1*2 + hidden_size_layer_2*2
        self.latent_length = ZDIMS

        self.hidden_to_linear = nn.Linear(self.hid_dim, self.hid_dim)
        self.hidden_to_mean = nn.Linear(self.hid_dim, self.latent_length)
        self.hidden_to_logvar = nn.Linear(self.hid_dim, self.latent_length)

        self.softplus = nn.Softplus()

    def forward(self, cell_output):
        """Given last hidden state of encoder, passes through a linear layer, and finds the mean and variance

        :param cell_output: last hidden state of encoder
        :return: latent vector
        """

        self.latent_mean = self.hidden_to_mean(cell_output)

        # based on Pereira et al 2019:
        # "The SoftPlus function ensures that the variance is parameterized as non-negative and activated
        # by a smooth function
        self.latent_logvar = self.softplus(self.hidden_to_logvar(cell_output))

        if self.training:
            std = self.latent_logvar.mul(0.5).exp_()
            eps = Variable(std.data.new(std.size()).normal_())
            return eps.mul(std).add_(self.latent_mean), self.latent_mean, self.latent_logvar
        else:
            return self.latent_mean, self.latent_mean, self.latent_logvar


class Decoder(nn.Module):
    def __init__(self,TEMPORAL_WINDOW,ZDIMS,NUM_FEATURES, hidden_size_rec, dropout_rec):
        super(Decoder,self).__init__()

        self.num_features = NUM_FEATURES
        self.sequence_length = TEMPORAL_WINDOW
        self.hidden_size = hidden_size_rec
        self.latent_length = ZDIMS
        self.n_layers  = 1
        self.dropout   = dropout_rec

        self.rnn_rec = nn.GRU(self.latent_length, hidden_size=self.hidden_size, num_layers=self.n_layers,
                            bias=True, batch_first=True, dropout=self.dropout, bidirectional=False)

        self.hidden_to_output = nn.Linear(self.hidden_size, self.num_features)

    def forward(self, inputs):
        decoder_output, _ = self.rnn_rec(inputs)
        prediction = self.hidden_to_output(decoder_output)

        return prediction

class Decoder_Future(nn.Module):
    def __init__(self,TEMPORAL_WINDOW,ZDIMS,NUM_FEATURES,FUTURE_STEPS, hidden_size_pred, dropout_pred):
        super(Decoder_Future,self).__init__()

        self.num_features = NUM_FEATURES
        self.future_steps = FUTURE_STEPS
        self.sequence_length = TEMPORAL_WINDOW
        self.hidden_size = hidden_size_pred
        self.latent_length = ZDIMS
        self.n_layers  = 1
        self.dropout   = dropout_pred

        self.rnn_pred = nn.GRU(self.latent_length, hidden_size=self.hidden_size, num_layers=self.n_layers,
                            bias=True, batch_first=True, dropout=self.dropout, bidirectional=True)

        self.hidden_to_output = nn.Linear(self.hidden_size*2, self.num_features)

    def forward(self, inputs):
        inputs = inputs[:,:self.future_steps,:]
        decoder_output, _ = self.rnn_pred(inputs)
        prediction = self.hidden_to_output(decoder_output)

        return prediction


class RNN_VAE(nn.Module):
    def __init__(self,TEMPORAL_WINDOW,ZDIMS,NUM_FEATURES,FUTURE_DECODER,FUTURE_STEPS, hidden_size_layer_1,
                        hidden_size_layer_2, hidden_size_rec, hidden_size_pred, dropout_encoder,
                        dropout_rec, dropout_pred):
        super(RNN_VAE,self).__init__()

        self.FUTURE_DECODER = FUTURE_DECODER
        self.seq_len = int(TEMPORAL_WINDOW / 2)
        self.encoder = Encoder(NUM_FEATURES, hidden_size_layer_1, hidden_size_layer_2, dropout_encoder)
        self.lmbda = Lambda(ZDIMS, hidden_size_layer_1, hidden_size_layer_2)
        self.decoder = Decoder(self.seq_len,ZDIMS,NUM_FEATURES, hidden_size_rec, dropout_rec)
        if FUTURE_DECODER:
            self.decoder_future = Decoder_Future(self.seq_len,ZDIMS,NUM_FEATURES,FUTURE_STEPS, hidden_size_pred,
                                                 dropout_pred)

    def forward(self,seq):

        """ Encode input sequence """
        h_n = self.encoder(seq)

        """ Compute the latent state via reparametrization trick """
        latent, mu, logvar = self.lmbda(h_n)
        z = latent.unsqueeze(2).repeat(1, 1, self.seq_len)
        z = z.permute(0,2,1)

        """ Predict the future of the sequence from the latent state"""
        prediction = self.decoder(z)

        if self.FUTURE_DECODER:
            future = self.decoder_future(z)
            return prediction, future, latent, mu, logvar
        else:
            return prediction, latent, mu, logvar


def reconstruction_loss(x, x_tilde, reduction):
    mse_loss = nn.MSELoss(reduction=reduction)
    rec_loss = mse_loss(x_tilde,x)
    return rec_loss

def future_reconstruction_loss(x, x_tilde, reduction):
    mse_loss = nn.MSELoss(reduction=reduction)
    rec_loss = mse_loss(x_tilde,x)
    return rec_loss

def cluster_loss(H, kloss, lmbda, batch_size):
    gram_matrix = (H.T @ H) / batch_size
    _ ,sv_2, _ = torch.svd(gram_matrix)
    sv = torch.sqrt(sv_2[:kloss])
    loss = torch.sum(sv)
    return lmbda*loss


def kullback_leibler_loss(mu, logvar):
    # see Appendix B from VAE paper:
        # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
        # https://arxiv.org/abs/1312.6114
        # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    # I'm using torch.mean() here as the sum() version depends on the size of the latent vector
    KLD = -0.5 * torch.mean(1 + logvar - mu.pow(2) - logvar.exp())
    return KLD


def kl_annealing(epoch, kl_start, annealtime, function):
    """
        Annealing of Kullback-Leibler loss to let the model learn first
        the reconstruction of the data before the KL loss term gets introduced.
    """
    if epoch > kl_start:
        if function == 'linear':
            new_weight = min(1, epoch/annealtime)

        elif function == 'sigmoid':
            new_weight = float(1/(1+np.exp(-0.9*(epoch-annealtime))))
        else:
            raise NotImplementedError('currently only "linear" and "sigmoid" are implemented')

        return new_weight

    else:
        new_weight = 0
        return new_weight


def gaussian(ins, is_training, seq_len, std_n=0.8):
    if is_training:
        emp_std = ins.std(1)*std_n
        emp_std = emp_std.unsqueeze(2).repeat(1, 1, seq_len)
        emp_std = emp_std.permute(0,2,1)
        noise = Variable(ins.data.new(ins.size()).normal_(0, 1))
        return ins + (noise*emp_std)
    return ins


def train(train_loader, epoch, model, optimizer, anneal_function, BETA, kl_start,
          annealtime, seq_len, future_decoder, future_steps, scheduler, mse_red, mse_pred, kloss, klmbda, bsize):
    model.train() # toggle model to train mode
    train_loss = 0.0
    mse_loss = 0.0
    kullback_loss = 0.0
    kmeans_losses = 0.0
    fut_loss = 0.0
    loss = 0.0
    seq_len_half = int(seq_len / 2)

    for idx, data_item in enumerate(train_loader):
        data_item = Variable(data_item)
        data_item = data_item.permute(0,2,1)

        if use_gpu:
            data = data_item[:,:seq_len_half,:].type('torch.FloatTensor').cuda()
            fut = data_item[:,seq_len_half:seq_len_half+future_steps,:].type('torch.FloatTensor').cuda()
        else:
            data = data_item[:,:seq_len_half,:].type('torch.FloatTensor').to()
            fut = data_item[:,seq_len_half:seq_len_half+future_steps,:].type('torch.FloatTensor').to()
        data_gaussian = gaussian(data,True,seq_len_half)

        if future_decoder:
            data_tilde, future, latent, mu, logvar = model(data_gaussian)

            rec_loss = reconstruction_loss(data, data_tilde, mse_red)
            fut_rec_loss = future_reconstruction_loss(fut, future, mse_pred)
            kmeans_loss = cluster_loss(latent.T, kloss, klmbda, bsize)
            kl_loss = kullback_leibler_loss(mu, logvar)
            kl_weight = kl_annealing(epoch, kl_start, annealtime, anneal_function)
            loss = rec_loss + fut_rec_loss + BETA*kl_weight*kl_loss + kl_weight*kmeans_loss
            fut_loss += fut_rec_loss.item()

        else:
            data_tilde, latent, mu, logvar = model(data_gaussian)

            rec_loss = reconstruction_loss(data, data_tilde, mse_red)
            kl_loss = kullback_leibler_loss(mu, logvar)
            kmeans_loss = cluster_loss(latent.T, kloss, klmbda, bsize)
            kl_weight = kl_annealing(epoch, kl_start, annealtime, anneal_function)
            loss = rec_loss + BETA*kl_weight*kl_loss + kl_weight*kmeans_loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm = 5)

        train_loss += loss.item()
        mse_loss += rec_loss.item()
        kullback_loss += kl_loss.item()
        kmeans_losses += kmeans_loss

        if idx % 1000 == 0:
            print('Epoch: %d.  loss: %.4f' %(epoch, loss.item()))

        scheduler.step() #be sure scheduler is called before optimizer in >1.1 pytorch

    if future_decoder:
        print('Average Train loss: {:.4f}, MSE-Loss: {:.4f}, MSE-Future-Loss {:.4f}, KL-Loss: {:.4f},  Kmeans-Loss: {:.4f}, weigt: {:.4f}'.format(train_loss / idx,
              mse_loss /idx, fut_loss/idx, BETA*kl_weight*kullback_loss/idx, kl_weight*kmeans_losses/idx, kl_weight))
    else:
        print('Average Train loss: {:.4f}, MSE-Loss: {:.4f}, KL-Loss: {:.4f}, Kmeans-Loss: {:.4f}, weight: {:.4f}'.format(train_loss / idx,
              mse_loss /idx, BETA*kl_weight*kullback_loss/idx, kl_weight*kmeans_losses/idx, kl_weight))

    return kl_weight, train_loss/idx, kl_weight*kmeans_losses/idx, kullback_loss/idx, mse_loss/idx, fut_loss/idx


def test(test_loader, epoch, model, optimizer, BETA, kl_weight, seq_len, mse_red, kloss, klmbda, future_decoder, bsize):
    model.eval() # toggle model to inference mode
    test_loss = 0.0
    mse_loss = 0.0
    kullback_loss = 0.0
    kmeans_losses = 0.0
    loss = 0.0
    seq_len_half = int(seq_len / 2)

    with torch.no_grad():
        for idx, data_item in enumerate(test_loader):
            # we're only going to infer, so no autograd at all required
            data_item = Variable(data_item)
            data_item = data_item.permute(0,2,1)
            if use_gpu:
                data = data_item[:,:seq_len_half,:].type('torch.FloatTensor').cuda()
            else:
                data = data_item[:,:seq_len_half,:].type('torch.FloatTensor').to()

            if future_decoder:
                recon_images, _, latent, mu, logvar = model(data)
                rec_loss = reconstruction_loss(data, recon_images, mse_red)
                kl_loss = kullback_leibler_loss(mu, logvar)
                kmeans_loss = cluster_loss(latent.T, kloss, klmbda, bsize)
                loss = rec_loss + BETA*kl_weight*kl_loss+ kl_weight*kmeans_loss

            else:
                recon_images, latent, mu, logvar = model(data)
                rec_loss = reconstruction_loss(data, recon_images, mse_red)
                kl_loss = kullback_leibler_loss(mu, logvar)
                kmeans_loss = cluster_loss(latent.T, kloss, klmbda, bsize)
                loss = rec_loss + BETA*kl_weight*kl_loss + kl_weight*kmeans_loss

            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm = 5)

            test_loss += loss.item()
            mse_loss += rec_loss.item()
            kullback_loss += kl_loss.item()
            kmeans_losses += kmeans_loss

    print('Average Test loss: {:.4f}, MSE-Loss: {:.4f}, KL-Loss: {:.4f}, Kmeans-Loss: {:.4f}'.format(test_loss / idx,
          mse_loss /idx, BETA*kl_weight*kullback_loss/idx, kl_weight*kmeans_losses/idx))

    return mse_loss /idx, test_loss/idx, kl_weight*kmeans_losses


def rnn_model(config, model_name, pretrained_weights=False, pretrained_model=None):
    config_file = Path(config).resolve()
    cfg = read_config(config_file)

    print("Train RNN model!")
    if not os.path.exists(cfg['project_path']+'/'+'model/best_model'):
        os.mkdir(cfg['project_path']+'/model/'+'best_model')
        os.mkdir(cfg['project_path']+'/model/'+'best_model/snapshots')
        os.mkdir(cfg['project_path']+'/model/'+'model_losses')

    # make sure torch uses cuda for GPU computing
    use_gpu = torch.cuda.is_available()
    if use_gpu:
        print("Using CUDA")
        print('GPU active:',torch.cuda.is_available())
        print('GPU used:',torch.cuda.get_device_name(0))
    else:
        torch.device("cpu")
        print("warning, a GPU was not found... proceeding with CPU (slow!)")
        #raise NotImplementedError('GPU Computing is required!')

    """ HYPERPARAMTERS """
    # General
    CUDA = use_gpu
    SEED = 19
    TRAIN_BATCH_SIZE = cfg['batch_size']
    TEST_BATCH_SIZE = int(cfg['batch_size']/4)
    EPOCHS = cfg['max_epochs']
    ZDIMS = cfg['zdims']
    BETA  = cfg['beta']
    SNAPSHOT = cfg['model_snapshot']
    LEARNING_RATE = cfg['learning_rate']
    NUM_FEATURES = cfg['num_features']
    TEMPORAL_WINDOW = cfg['time_window']*2
    FUTURE_DECODER = cfg['prediction_decoder']
    FUTURE_STEPS = cfg['prediction_steps']

    # RNN
    hidden_size_layer_1 = cfg['hidden_size_layer_1']
    hidden_size_layer_2 = cfg['hidden_size_layer_2']
    hidden_size_rec = cfg['hidden_size_rec']
    hidden_size_pred = cfg['hidden_size_pred']
    dropout_encoder = cfg['dropout_encoder']
    dropout_rec = cfg['dropout_rec']
    dropout_pred = cfg['dropout_pred']

    # Loss
    MSE_REC_REDUCTION = cfg['mse_reconstruction_reduction']
    MSE_PRED_REDUCTION = cfg['mse_prediction_reduction']
    KMEANS_LOSS = cfg['kmeans_loss']
    KMEANS_LAMBDA = cfg['kmeans_lambda']
    KL_START = cfg['kl_start']
    ANNEALTIME = cfg['annealtime']
    anneal_function = cfg['anneal_function']
    optimizer_scheduler = cfg['scheduler']

    BEST_LOSS = 999999
    convergence = 0
    print('Latent Dimensions: %d, Beta: %d, lr: %.4f' %(ZDIMS, BETA, LEARNING_RATE))

    # simple logging of diverse losses
    train_losses = []
    test_losses = []
    kmeans_losses = []
    kl_losses = []
    weight_values = []
    mse_losses = []
    fut_losses = []

    torch.manual_seed(SEED)
    if CUDA:
        torch.cuda.manual_seed(SEED)
        model = RNN_VAE(TEMPORAL_WINDOW,ZDIMS,NUM_FEATURES,FUTURE_DECODER,FUTURE_STEPS, hidden_size_layer_1,
                        hidden_size_layer_2, hidden_size_rec, hidden_size_pred, dropout_encoder,
                        dropout_rec, dropout_pred).cuda()
    else: #cpu support ...
        torch.cuda.manual_seed(SEED)
        model = RNN_VAE(TEMPORAL_WINDOW,ZDIMS,NUM_FEATURES,FUTURE_DECODER,FUTURE_STEPS, hidden_size_layer_1,
                        hidden_size_layer_2, hidden_size_rec, hidden_size_pred, dropout_encoder,
                        dropout_rec, dropout_pred).to()

    if pretrained_weights:
        if os.path.exists(cfg['project_path']+'/'+'model/'+'pretrained_model/'+pretrained_model+'.pkl'): #TODO, fix this path seeking....
            print("Loading pretrained Model: %s" %pretrained_model)
            model.load_state_dict(torch.load(cfg['project_path']+'/'+'model/'+'pretrained_model/'+pretrained_model+'.pkl'), strict=False)

    """ DATASET """
    trainset = SEQUENCE_DATASET(os.path.join(cfg['project_path'],"data", "train",""), data='train_seq.npy', train=True, temporal_window=TEMPORAL_WINDOW)
    testset = SEQUENCE_DATASET(os.path.join(cfg['project_path'],"data", "train",""), data='test_seq.npy', train=False, temporal_window=TEMPORAL_WINDOW)

    train_loader = Data.DataLoader(trainset, batch_size=TRAIN_BATCH_SIZE, shuffle=True, drop_last=True)
    test_loader = Data.DataLoader(testset, batch_size=TEST_BATCH_SIZE, shuffle=True, drop_last=True)

    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE, amsgrad=True)

    if optimizer_scheduler:
        scheduler = StepLR(optimizer, step_size=100, gamma=0.2, last_epoch=-1)
    else:
        scheduler = StepLR(optimizer, step_size=100, gamma=0, last_epoch=-1)

    for epoch in range(1,EPOCHS):
        print('Epoch: %d' %epoch)
        print('Train: ')
        weight, train_loss, km_loss, kl_loss, mse_loss, fut_loss = train(train_loader, epoch, model, optimizer,
                                                                         anneal_function, BETA, KL_START,
                                                                         ANNEALTIME, TEMPORAL_WINDOW, FUTURE_DECODER,
                                                                         FUTURE_STEPS, scheduler, MSE_REC_REDUCTION,
                                                                         MSE_PRED_REDUCTION, KMEANS_LOSS, KMEANS_LAMBDA,
                                                                         TRAIN_BATCH_SIZE)

        print('Test: ')
        current_loss, test_loss, test_list = test(test_loader, epoch, model, optimizer,
                                                  BETA, weight, TEMPORAL_WINDOW, MSE_REC_REDUCTION,
                                                  KMEANS_LOSS, KMEANS_LAMBDA, FUTURE_DECODER, TEST_BATCH_SIZE)

        for param_group in optimizer.param_groups:
            print('lr: {}'.format(param_group['lr']))
        # logging losses
        train_losses.append(train_loss)
        test_losses.append(test_loss)
        kmeans_losses.append(km_loss)
        kl_losses.append(kl_loss)
        weight_values.append(weight)
        mse_losses.append(mse_loss)
        fut_losses.append(fut_loss)

        # save best model
        if weight > 0.99 and current_loss <= BEST_LOSS:
            BEST_LOSS = current_loss
            print("Saving model!\n")
            torch.save(model.state_dict(), cfg['project_path']+'/'+'model/'+'best_model'+'/'+model_name+'_'+cfg['Project']+'.pkl')
            convergence = 0
        else:
            convergence += 1

        if epoch % SNAPSHOT == 0:
            print("Saving model snapshot!\n")
            torch.save(model.state_dict(), cfg['project_path']+'/'+'model/'+'best_model'+'/snapshots/'+model_name+'_'+cfg['Project']+'_epoch_'+str(epoch)+'.pkl')

        if convergence > cfg['model_convergence']:
            print('Model converged. Please check your model with vame.evaluate_model(). \n'
                  'You can also re-run vame.rnn_model() to further improve your model. \n'
                  'Hint: Set "model_convergence" in your config.yaml to a higher value. \n'
                  '\n'
                  'Next: \n'
                  'Use vame.behavior_segmentation() to identify behavioral motifs in your dataset!')
            #return
            break

        # save logged losses
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/train_losses_'+model_name, train_losses)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/test_losses_'+model_name, test_losses)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/kmeans_losses_'+model_name, kmeans_losses)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/kl_losses_'+model_name, kl_losses)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/weight_values_'+model_name, weight_values)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/mse_train_losses_'+model_name, mse_losses)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/mse_test_losses_'+model_name, current_loss)
        np.save(cfg['project_path']+'/'+'model/'+'model_losses'+'/fut_losses_'+model_name, fut_losses)


    if convergence < cfg['model_convergence']:
        print('Model seemed to have not reached convergence. You may want to check your model \n'
              'with vame.evaluate_model(). If your satisfied you can continue with \n'
              'Use vame.behavior_segmentation() to identify behavioral motifs!\n\n'
              'OPTIONAL: You can re-run vame.rnn_model() to improve performance.')
