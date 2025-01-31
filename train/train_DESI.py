#!/usr/bin/env python

import argparse
import functools
import os
import time
from tqdm import tqdm

import numpy as np
import torch
from torch import nn
from accelerate import Accelerator
from spender import SpectrumAutoencoder
from spender.data import desi
from spender.util import mem_report, resample_to_restframe

# allows one to run fp16_train.py from home directory
import sys;sys.path.insert(1, './')

def prepare_train(seq,niter=800):
    for d in seq:
        if not "iteration" in d:d["iteration"]=niter
        if not "encoder" in d:d.update({"encoder":d["data"]})
    return seq

def build_ladder(train_sequence):
    n_iter = sum([item['iteration'] for item in train_sequence])

    ladder = np.zeros(n_iter,dtype='int')
    n_start = 0
    for i,mode in enumerate(train_sequence):
        n_end = n_start+mode['iteration']
        ladder[n_start:n_end]= i
        n_start = n_end
    return ladder

def get_all_parameters(models,instruments):
    model_params = []
    # multiple encoders
    for model in models:
        model_params += model.encoder.parameters()
    # 1 decoder
    model_params += model.decoder.parameters()
    dicts = [{'params':model_params}]

    n_parameters = sum([p.numel() for p in model_params if p.requires_grad])

    instr_params = []
    # instruments
    for inst in instruments:
        if inst==None:continue
        instr_params += inst.parameters()
        s = [p.numel() for p in inst.parameters()]
    if instr_params != []:
        dicts.append({'params':instr_params,'lr': 1e-4})
        n_parameters += sum([p.numel() for p in instr_params if p.requires_grad])
        print("parameter dict:",dicts[1])
    return dicts,n_parameters

def consistency_loss(s, s_aug, individual=False):
    batch_size, s_size = s.shape
    x = torch.sum((s_aug - s)**2/(0.5)**2,dim=1)/s_size
    sim_loss = torch.sigmoid(x)-0.5 # zero = perfect alignment
    if individual:
        return x, sim_loss
    return sim_loss.sum()

def similarity_loss(instrument, model, spec, w, z, s, slope=0.5, individual=False, wid=5, amp=3):
    spec,w = resample_to_restframe(instrument.wave_obs,
                                   model.decoder.wave_rest,
                                   spec,w,z)

    batch_size, spec_size = spec.shape
    _, s_size = s.shape
    device = s.device

    # pairwise dissimilarity of spectra
    S = (spec[None,:,:] - spec[:,None,:])**2

    # pairwise weights
    non_zero = w > 1e-6
    N = (non_zero[None,:,:] * non_zero[:,None,:])
    W = (1 / w)[None,:,:] + (1 / w)[:,None,:]
    W =  N / W

    N = N.sum(-1)
    N[N==0] = 1
    # dissimilarity of spectra
    # of order unity, larger for spectrum pairs with more comparable bins
    spec_sim = (W * S).sum(-1) / N

    # dissimilarity of latents
    s_sim = ((s[None,:,:] - s[:,None,:])**2).sum(-1) / s_size

    # only give large loss of (dis)similarities are different (either way)
    x = s_sim-spec_sim
    sim_loss = torch.sigmoid(slope*x-0.5*wid)+torch.sigmoid(-slope*x-0.5*wid)
    diag_mask = torch.diag(torch.ones(batch_size,device=device,dtype=bool))
    sim_loss[diag_mask] = 0

    if individual:
        return s_sim,spec_sim,sim_loss
    # total loss: sum over N^2 terms,
    # needs to have amplitude of N terms to compare to fidelity loss
    return amp*sim_loss.sum() / batch_size

def restframe_weight(model,mu=5000,sigma=2000,amp=30):
    x = model.decoder.wave_rest
    return amp*torch.exp(-(0.5*(x-mu)/sigma)**2)

def similarity_restframe(instrument, model, s=None, slope=1.0,
                         individual=False, wid=5, bound=[4000,7000]):
    _, s_size = s.shape
    device = s.device

    spec = model.decode(s)
    wave = model.decoder.wave_rest
    mask = (wave>bound[0])*(wave<bound[1])
    spec /= spec[:,mask].median(dim=1)[0][:,None]
    batch_size, spec_size = spec.shape
    # pairwise dissimilarity of spectra
    S = (spec[None,:,:] - spec[:,None,:])**2
    # dissimilarity of spectra
    # of order unity, larger for spectrum pairs with more comparable bins
    W = restframe_weight(model)
    spec_sim = (W * S).sum(-1) / spec_size
    # dissimilarity of latents
    s_sim = ((s[None,:,:] - s[:,None,:])**2).sum(-1) / s_size

    # only give large loss of (dis)similarities are different (either way)
    x = s_sim-spec_sim
    sim_loss = torch.sigmoid(slope*x-wid/2)+torch.sigmoid(-slope*x-wid/2)
    diag_mask = torch.diag(torch.ones(batch_size,device=device,dtype=bool))
    sim_loss[diag_mask] = 0

    if individual:
        return s_sim,spec_sim,sim_loss

    # total loss: sum over N^2 terms,
    # needs to have amplitude of N terms to compare to fidelity loss
    return sim_loss.sum() / batch_size

def _losses(model,
            instrument,
            batch,
            similarity=True,
            slope=0,
            skip=False
           ):

    spec, w, z = batch
    # need the latents later on if similarity=True
    s = model.encode(spec)
    if skip: return 0,0,s
    loss = model.loss(spec, w, instrument, z=z, s=s)

    if similarity:
        sim_loss = similarity_restframe(instrument, model, s, slope=slope)
    else: sim_loss = 0

    return loss, sim_loss, s

def get_losses(model,
               instrument,
               batch,
               aug_fct=None,
               similarity=True,
               consistency=True,
               slope=0
               ):

    loss, sim_loss, s = _losses(model, instrument, batch, similarity=similarity, slope=slope)

    if aug_fct is not None:
        z_max = 0.8 # Hack, variable not passed.
        batch_copy = aug_fct(batch,z_max=z_max)
        loss_, sim_loss_, s_ = _losses(model, instrument, batch_copy, similarity=similarity, slope=slope,skip=True)
    else:
        loss_ = sim_loss_ = 0

    if consistency and aug_fct is not None:
        cons_loss = slope*consistency_loss(s, s_)
    else:
        cons_loss = 0

    #from IPython.core import debugger as ipdb
    #if slope > 0:
    #    ipdb.set_trace()

    return loss, sim_loss, loss_, sim_loss_, cons_loss


def checkpoint(accelerator, args, optimizer, scheduler, n_encoder, outfile, losses):
    unwrapped = [accelerator.unwrap_model(args_i).state_dict() for args_i in args]

    accelerator.save({
        "model": unwrapped,
        "losses": losses,
    }, outfile)
    return

def load_model(filename, models, instruments):
    device = instruments[0].wave_obs.device
    model_struct = torch.load(filename, map_location=device)
    #wave_rest = model_struct['model'][0]['decoder.wave_rest']
    for i, model in enumerate(models):
        # backwards compat: encoder.mlp instead of encoder.mlp.mlp
        if 'encoder.mlp.mlp.0.weight' in model_struct['model'][i].keys():
            from collections import OrderedDict
            model_struct['model'][i] = OrderedDict([(k.replace('mlp.mlp', 'mlp'), v) for k, v in model_struct['model'][i].items()])
        # backwards compat: add instrument to encoder
        try:
            model.load_state_dict(model_struct['model'][i], strict=False)
        except RuntimeError:
            model_struct['model'][i]['encoder.instrument.wave_obs']= instruments[i].wave_obs
            model_struct['model'][i]['encoder.instrument.skyline_mask']= instruments[i].skyline_mask
            model.load_state_dict(model_struct[i]['model'], strict=False)

    losses = model_struct['losses']
    return models, losses


def train(models,
          instruments,
          trainloaders,
          validloaders,
          train_sequence=None,
          ANNEAL_SCHEDULE=None,
          n_epoch=200,
          outfile=None,
          losses=None,
          verbose=False,
          lr=1e-4,
          n_batch=50,
          aug_fcts=None,
          similarity=True,
          consistency=True,
          ):

    n_encoder = len(models)
    model_parameters, n_parameters = get_all_parameters(models,instruments)

    if verbose:
        print("model parameters:", n_parameters)
        mem_report()

    ladder = build_ladder(train_sequence)
    optimizer = torch.optim.Adam(model_parameters, lr=lr, eps=1e-4)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, lr,
                                              total_steps=n_epoch)

    accelerator = Accelerator(mixed_precision='fp16')
    models = [accelerator.prepare(model) for model in models]
    instruments = [accelerator.prepare(instrument) for instrument in instruments]
    trainloaders = [accelerator.prepare(loader) for loader in trainloaders]
    validloaders = [accelerator.prepare(loader) for loader in validloaders]
    optimizer = accelerator.prepare(optimizer)

    # define losses to track
    n_loss = 5
    epoch = 0
    if losses is None:
        detailed_loss = np.zeros((2, n_encoder, n_epoch, n_loss))
    else:
        try:
            epoch = len(losses[0][0])
            n_epoch += epoch
            detailed_loss = np.zeros((2, n_encoder, n_epoch, n_loss))
            detailed_loss[:, :, :epoch, :] = losses
            if verbose:
                losses = tuple(detailed_loss[0, :, epoch-1, :])
                vlosses = tuple(detailed_loss[1, :, epoch-1, :])
                print(f'====> Epoch: {epoch-1}')
                print('TRAINING Losses:', losses)
                print('VALIDATION Losses:', vlosses)
        except: # OK if losses are empty
            pass

    if outfile is None:
        outfile = "checkpoint.pt"

    for epoch_ in range(epoch, n_epoch):

        mode = train_sequence[ladder[epoch_ - epoch]]

        # turn on/off model decoder
        for p in models[0].decoder.parameters():
            p.requires_grad = mode['decoder']

        slope = ANNEAL_SCHEDULE[(epoch_ - epoch)%len(ANNEAL_SCHEDULE)]
        if n_epoch-epoch_<=10: slope=0 # turn off similarity

        if verbose and similarity:
            print("similarity info:",slope)

        for which in range(n_encoder):

            # turn on/off encoder
            for p in models[which].encoder.parameters():
                p.requires_grad = mode['encoder'][which]

            # optional: training on single dataset
            if not mode['data'][which]:
                continue

            models[which].train()
            instruments[which].train()

            n_sample = 0
            for k, batch in tqdm(enumerate(trainloaders[which])):
                batch_size = len(batch[0])
                losses = get_losses(
                    models[which],
                    instruments[which],
                    batch,
                    aug_fct=aug_fcts[which],
                    similarity=similarity,
                    consistency=consistency,
                    slope=slope,
                )
                # sum up all losses
                loss = functools.reduce(lambda a, b: a+b , losses)
                accelerator.backward(loss)
                # clip gradients: stabilizes training with similarity
                accelerator.clip_grad_norm_(model_parameters[0]['params'], 1.0)
                # once per batch
                optimizer.step()
                optimizer.zero_grad()

                # logging: training
                detailed_loss[0][which][epoch_] += tuple( l.item() if hasattr(l, 'item') else 0 for l in losses )
                n_sample += batch_size

                # stop after n_batch
                if n_batch is not None and k == n_batch - 1:
                    break
            detailed_loss[0][which][epoch_] /= n_sample

        scheduler.step()

        with torch.no_grad():
            for which in range(n_encoder):
                models[which].eval()
                instruments[which].eval()

                n_sample = 0
                for k, batch in enumerate(validloaders[which]):
                    batch_size = len(batch[0])
                    losses = get_losses(
                        models[which],
                        instruments[which],
                        batch,
                        aug_fct=aug_fcts[which],
                        similarity=similarity,
                        consistency=consistency,
                        slope=slope,
                    )
                    # logging: validation
                    detailed_loss[1][which][epoch_] += tuple( l.item() if hasattr(l, 'item') else 0 for l in losses )
                    n_sample += batch_size

                    # stop after n_batch
                    if n_batch is not None and k == n_batch - 1:
                        break

                detailed_loss[1][which][epoch_] /= n_sample

        if verbose:
            mem_report()
            losses = tuple(detailed_loss[0, :, epoch_, :])
            vlosses = tuple(detailed_loss[1, :, epoch_, :])
            print('====> Epoch: %i'%(epoch))
            print('TRAINING Losses:', losses)
            print('VALIDATION Losses:', vlosses)

        if epoch_ % 5 == 0 or epoch_ == n_epoch - 1:
            args = models
            checkpoint(accelerator, args, optimizer, scheduler, n_encoder, outfile, detailed_loss)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("dir", help="data file directory")
    parser.add_argument("outfile", help="output file name")
    parser.add_argument("-n", "--latents", help="latent dimensionality", type=int, default=2)
    parser.add_argument("-b", "--batch_size", help="batch size", type=int, default=512)
    parser.add_argument("-l", "--batch_number", help="number of batches per epoch", type=int, default=None)
    parser.add_argument("-r", "--rate", help="learning rate", type=float, default=1e-3)
    parser.add_argument("-zmax", "--z_max", help="constrain redshifts to z_max", type=float, default=0.8)
    parser.add_argument("-a", "--augmentation", help="add augmentation loss", action="store_true")
    parser.add_argument("-s", "--similarity", help="add similarity loss", action="store_true")
    parser.add_argument("-c", "--consistency", help="add consistency loss", action="store_true")
    parser.add_argument("-C", "--clobber", help="continue training of existing model", action="store_true")
    parser.add_argument("-v", "--verbose", help="verbose printing", action="store_true")
    args = parser.parse_args()

    # define instruments
    instruments = [ desi.DESI() ]
    n_encoder = len(instruments)

    # restframe wavelength for reconstructed spectra
    # Note: represents joint dataset wavelength range
    if args.z_max > 0.01:# DESI BGS
        lmbda_min = instruments[0].wave_obs[0]/(1.0+args.z_max) # 2000 A
        lmbda_max = instruments[0].wave_obs[-1] # 9824 A
        bins = 9780
    else: # DESI MWS
        lmbda_min = instruments[0].wave_obs[0]/(1.0+args.z_max)
        lmbda_max = instruments[0].wave_obs[-1]/(1.0-args.z_max)
        bins = int((lmbda_max-lmbda_min).item()/0.8)
    wave_rest = torch.linspace(lmbda_min, lmbda_max, bins, dtype=torch.float32)
    
    if args.verbose:
        print ("Restframe:\t{:.0f} .. {:.0f} A ({} bins)".format(lmbda_min, lmbda_max, bins))

    # data loaders
    trainloaders = [ inst.get_data_loader(args.dir, tag="Stars", which="train",  batch_size=args.batch_size, shuffle=True, shuffle_instance=True) for inst in instruments ]
    validloaders = [ inst.get_data_loader(args.dir,  tag="Stars", which="valid", batch_size=args.batch_size, shuffle=True, shuffle_instance=True) for inst in instruments ]

    # get augmentation function
    if args.augmentation:
        aug_fcts = [ desi.DESI().augment_spectra ]
    else:
        aug_fcts = [ None ]

    # define training sequence
    FULL = {"data":[True],"decoder":True}
    train_sequence = prepare_train([FULL])

    annealing_step = 0.1
    ANNEAL_SCHEDULE = np.arange(0.0,2.0,annealing_step)

    if args.verbose and args.similarity:
        print("similarity_slope:",len(ANNEAL_SCHEDULE),ANNEAL_SCHEDULE)

    # define and train the model
    n_hidden = (64, 256, 1024)
    models = [ SpectrumAutoencoder(instrument,
                                   wave_rest,
                                   n_latent=args.latents,
                                   n_hidden=n_hidden,
                                   act=[nn.LeakyReLU()]*(len(n_hidden)+1)
                                   )
              for instrument in instruments ]
    # use same decoder
    if n_encoder==2:models[1].decoder = models[0].decoder

    n_epoch = sum([item['iteration'] for item in train_sequence])
    init_t = time.time()
    if args.verbose:
        print("torch.cuda.device_count():",torch.cuda.device_count())
        print (f"--- Model {args.outfile} ---")

    # check if outfile already exists, continue only of -c is set
    if os.path.isfile(args.outfile) and not args.clobber:
        raise SystemExit("\nOutfile exists! Set option -C to continue training.")
    losses = None
    if os.path.isfile(args.outfile):
        if args.verbose:
            print (f"\nLoading file {args.outfile}")
        model, losses = load_model(args.outfile, models, instruments)
        non_zero = np.sum(losses[0][0],axis=1)>0
        losses = losses[:,:,non_zero,:]

    train(models, instruments, trainloaders, validloaders, n_epoch=n_epoch,
          n_batch=args.batch_number, lr=args.rate, aug_fcts=aug_fcts, similarity=args.similarity, consistency=args.consistency, outfile=args.outfile, losses=losses, verbose=args.verbose)

    if args.verbose:
        print("--- %s seconds ---" % (time.time()-init_t))
