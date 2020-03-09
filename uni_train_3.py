import json
import torch
import os
import numpy as np
from uni_model_2 import *
# from data_loader import MusicArrayLoader
from torch import optim
from torch.distributions import kl_divergence, Normal
from torch.nn import functional as F
from torch.optim.lr_scheduler import ExponentialLR
from sklearn.model_selection import train_test_split

from ptb_v2 import *


# some initialization
with open('uni_model_config_2.json') as f:
    args = json.load(f)
if not os.path.isdir('log'):
    os.mkdir('log')
if not os.path.isdir('params'):
    os.mkdir('params')
save_path = 'params/{}.pt'.format(args['name'])

from datetime import datetime
timestamp = str(datetime.now())
save_path_timing = 'params/{}.pt'.format(args['name'] + "_" + timestamp)

# model dimensions
EVENT_DIMS = 342
RHYTHM_DIMS = 3
NOTE_DIMS = 16
TEMPO_DIMS = 264
VELOCITY_DIMS = 126
CHROMA_DIMS = 24

is_adversarial = args["is_adversarial"]
print("Is adversarial: {}".format(is_adversarial))

model = MusicGenericAttrRegVAE(roll_dims=EVENT_DIMS,
                    attr_dims_lst=[RHYTHM_DIMS, NOTE_DIMS, CHROMA_DIMS],
                    hidden_dims=args['hidden_dim'], z_dims=args['z_dim'], 
                    n_step=args['time_step'])

if os.path.exists(save_path):
    print("Loading {}".format(save_path))
    model.load_state_dict(torch.load(save_path))
else:
    print("Save path: {}".format(save_path))

if args['if_parallel']:
    model = torch.nn.DataParallel(model, device_ids=[0, 1])

optimizer = optim.Adam(model.parameters(), lr=args['lr'])

if torch.cuda.is_available():
    print('Using: ', torch.cuda.get_device_name(torch.cuda.current_device()))
    model.cuda()
else:
    print('CPU mode')

step, pre_epoch = 0, 0
batch_size = args["batch_size"]
model.train()

# dataloaders
is_shuffle = True
data_lst, rhythm_lst, note_density_lst, chroma_lst = get_classic_piano()
tlen, vlen = int(0.8 * len(data_lst)), int(0.9 * len(data_lst))
train_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="train")
train_dl_dist = DataLoader(train_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
val_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="val")
val_dl_dist = DataLoader(val_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
test_ds_dist = MusicAttrDataset2(data_lst, rhythm_lst, note_density_lst, 
                                chroma_lst, mode="test")
test_dl_dist = DataLoader(test_ds_dist, batch_size=batch_size, shuffle=is_shuffle, num_workers=0)
dl = train_dl_dist
print(len(train_ds_dist), len(val_ds_dist), len(test_ds_dist))


is_class = args["is_class"]
is_res = args["is_res"]
# end of initialization


def std_normal(shape):
    N = Normal(torch.zeros(shape), torch.ones(shape))
    if torch.cuda.is_available():
        N.loc = N.loc.cuda()
        N.scale = N.scale.cuda()
    return N


def loss_function(out, d,
                sub_output_lst, sub_actual_lst,
                dis,
                step,
                beta=.1):

    CE_X = F.nll_loss(out.view(-1, out.size(-1)),
                    d.view(-1), reduction='mean')
    
    CE_sub = 0
    for i in range(len(sub_output_lst)):
        CE_sub += F.nll_loss(sub_output_lst[i].view(-1, sub_output_lst[i].size(-1)),
                    sub_actual_lst[i].view(-1), reduction='mean')

    CE = 5 * CE_X + CE_sub     # speed up reconstruction training

    # all distribution conform to standard gaussian
    inputs = dis
    KLD = 0
    for input in inputs:
        normal = std_normal(input.mean.size())
        KLD += kl_divergence(input, normal).mean()

    # anneal beta
    # beta0 = min(step / 3000 * beta, beta)  
    beta0 = beta
    return CE + beta0 * KLD, CE_X, CE_sub


def latent_regularized_loss_function(z_out, r, n):

    # # tanh on both terms
    return latent_regularized_loss_function_v1(z_out, r, n)

    # no tanh on both terms
    # return latent_regularized_loss_function_v5(z_out, r, n)
    
    # hybrid
    # l_r_1, l_n_1 = latent_regularized_loss_function_v1(z_out, r, n)
    # l_r_2, l_n_2 = latent_regularized_loss_function_v2(z_out, r, n)
    
    # # lambda1 is Pati's one, lambda2 is MSE's one
    # lambda1, lambda2 = 0.5, 0.5
    # l_r = lambda1 * l_r_1 + lambda2 * l_r_2
    # l_n = lambda1 * l_n_1 + lambda2 * l_n_2

    # return l_r, l_n


def latent_regularized_loss_function_v1(z_out, r, n):
    # regularization loss - Pati et al. 2019
    z_r, z_n = z_out

    # rhythm regularized
    r_density = [Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]
    D_attr_r = torch.from_numpy(np.subtract.outer(r_density, r_density)).cuda().float()
    D_z_r = z_r[:, 0].reshape(-1, 1) - z_r[:, 0]
    l_r = torch.nn.MSELoss(reduction="mean")(torch.tanh(D_z_r), torch.sign(D_attr_r))
    
    # note regularized
    def round_nearest(value):
        int_val = int(value)
        candidates = [int_val, int_val + 0.5, int_val + 1]
        diff = [abs(value - k) for k in candidates]
        candidate_idx = diff.index(min(diff))
        return candidates[candidate_idx]
        
    n_density = [round_nearest(sum(k.cpu().detach().numpy()) / len(k)) for k in n]
    D_attr_n = torch.from_numpy(np.subtract.outer(n_density, n_density)).cuda().float()
    D_z_n = z_n[:, 0].reshape(-1, 1) - z_n[:, 0]
    l_n = torch.nn.MSELoss(reduction="mean")(torch.tanh(D_z_n), torch.sign(D_attr_n))

    return l_r, l_n


def latent_regularized_loss_function_v2(z_out, r, n):
    # regularization loss, using simple MSE
    z_r, z_n = z_out

    # rhythm regularized
    r_density = torch.Tensor([Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]).cuda()
    l_r = torch.nn.MSELoss(reduction="mean")(z_r[:, 0], r_density)

    # note regularized
    n_density = torch.Tensor([sum(k.cpu().detach().numpy()) / len(k) for k in n]).cuda()
    l_n = torch.nn.MSELoss(reduction="mean")(z_n[:, 0], n_density)

    return l_r, l_n


def latent_regularized_loss_function_v3(z_out, r, n):
    z_r, z_n = z_out
    r_density = torch.Tensor([Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]).cuda()
    n_density = torch.Tensor([sum(k.cpu().detach().numpy()) / len(k) for k in n]).cuda()

    # prepare data
    z_r_0 = z_r[:, 0]
    D_z_r = z_r_0.view(-1, 1).repeat(1, z_r_0.shape[0])

    z_r_diff_sign = (D_z_r - D_z_r.transpose(1, 0)).view(-1, 1)
    z_r_diff_sign = torch.tanh(z_r_diff_sign * 10)
    
    # prepare labels
    D_r_density = r_density.view(-1, 1).repeat(1, r_density.shape[0])
    r_density_diff_sign = torch.sign(D_r_density - D_r_density.transpose(1, 0)).view(-1, 1)
    l_r = torch.nn.L1Loss(reduction="mean")(z_r_diff_sign, r_density_diff_sign)

    # prepare data
    z_n_0 = z_n[:, 0]
    D_z_n = z_n_0.view(-1, 1).repeat(1, z_n_0.shape[0])
    z_n_diff_sign = (D_z_n - D_z_n.transpose(1, 0)).view(-1, 1)
    z_n_diff_sign = torch.tanh(z_n_diff_sign * 10)
    
    # prepare labels
    D_n_density = n_density.view(-1, 1).repeat(1, n_density.shape[0])
    n_density_diff_sign = torch.sign(D_n_density - D_n_density.transpose(1, 0)).view(-1, 1)
    l_n = torch.nn.L1Loss(reduction="mean")(z_n_diff_sign, n_density_diff_sign)

    return l_r, l_n


def latent_regularized_loss_function_v4(z_out, r, n):
    # regularization loss - Pati et al. 2019
    z_r, z_n = z_out

    # rhythm regularized
    r_density = [Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]
    D_attr_r = torch.from_numpy(np.subtract.outer(r_density, r_density)).cuda().float()
    D_z_r = z_r[:, 0].reshape(-1, 1) - z_r[:, 0]
    l_r = torch.nn.MSELoss(reduction="mean")(torch.tanh(D_z_r), torch.tanh(D_attr_r))
    
    # note regularized
    n_density = [sum(k.cpu().detach().numpy()) / len(k) for k in n]
    D_attr_n = torch.from_numpy(np.subtract.outer(n_density, n_density)).cuda().float()
    D_z_n = z_n[:, 0].reshape(-1, 1) - z_n[:, 0]
    l_n = torch.nn.MSELoss(reduction="mean")(torch.tanh(D_z_n), torch.tanh(D_attr_n))

    return l_r, l_n


def latent_regularized_loss_function_v5(z_out, r, n):
    # regularization loss - Pati et al. 2019
    z_r, z_n = z_out

    # rhythm regularized
    r_density = [Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]
    D_attr_r = torch.from_numpy(np.subtract.outer(r_density, r_density)).cuda().float()
    D_z_r = z_r[:, 0].reshape(-1, 1) - z_r[:, 0]
    l_r = torch.nn.MSELoss(reduction="mean")(D_z_r, D_attr_r)
    
    # note regularized
    n_density = [sum(k.cpu().detach().numpy()) / len(k) for k in n]
    D_attr_n = torch.from_numpy(np.subtract.outer(n_density, n_density)).cuda().float()
    D_z_n = z_n[:, 0].reshape(-1, 1) - z_n[:, 0]
    l_n = torch.nn.MSELoss(reduction="mean")(D_z_n, D_attr_n)

    return l_r, l_n



def adversarial_loss(r_r_density, r_n_density, r, n):
    r_density = torch.Tensor([Counter(k.cpu().detach().numpy())[1] / len(k) for k in r]).cuda()
    n_density = torch.Tensor([sum(k.cpu().detach().numpy()) / len(k) for k in n]).cuda()

    l_adv_r = torch.nn.MSELoss(reduction="mean")(r_r_density.squeeze(), r_density)
    l_adv_n = torch.nn.MSELoss(reduction="mean")(r_n_density.squeeze(), n_density)
    return l_adv_r, l_adv_n


def class_loss_function(cls_r, c_r, cls_n, c_n):
    
    CE_R = torch.nn.CrossEntropyLoss(reduction='mean')(cls_r, c_r)
    CE_N = torch.nn.CrossEntropyLoss(reduction='mean')(cls_n, c_n)
    return CE_R + CE_N


def train(step, d_oh, r_oh, n_oh,
          d, r, n, c, c_r, c_n, c_r_oh, c_n_oh):
    
    optimizer.zero_grad()
    res = model(d_oh, [r_oh, n_oh, c])

    out, dis, z_out, sub_output_lst = res
        
    # calculate loss
    loss, CE_X, CE_sub = loss_function(out, d,
                                    sub_output_lst, [r, n],
                                    dis,
                                    step,
                                    beta=args['beta'])
    
    l_r, l_n = latent_regularized_loss_function(z_out, r, n)
    loss += l_r + l_n

    # adversarial loss
    if is_adversarial:
        adv_lambda = min(min(5000, step) / 5000, 0.15)
        l_adv_r, l_adv_n = adversarial_loss(r_r_density, r_n_density, r, n)
        loss += adv_lambda * (l_adv_r + l_adv_n)
    else:
        l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])
    
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
    optimizer.step()
    step += 1
    
    output = loss.item(), CE_X.item(), CE_sub.item(), l_r.item(), l_n.item(), \
                l_adv_r.item(), l_adv_n.item()
    return step, output


def evaluate(d_oh, r_oh, n_oh,
             d, r, n, c, c_r, c_n, c_r_oh, c_n_oh):
    
    res = model(d_oh, [r_oh, n_oh, c])

    out, dis, z_out, sub_output_lst = res

    # calculate loss
    loss, CE_X, CE_sub = loss_function(out, d,
                                            sub_output_lst, [r, n],
                                            dis,
                                            step,
                                            beta=args['beta'])

    l_r, l_n = latent_regularized_loss_function(z_out, r, n)
    loss += l_r + l_n

    # adversarial loss
    if is_adversarial:
        l_adv_r, l_adv_n = adversarial_loss(r_r_density, r_n_density, r, n)
        loss += l_adv_r + l_adv_n
    else:
        l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])
    
    output = loss.item(), CE_X.item(), CE_sub.item(), l_r.item(), l_n.item(), \
                l_adv_r.item(), l_adv_n.item()
    return output


def convert_to_one_hot(input, dims):
    if len(input.shape) > 1:
        input_oh = torch.zeros((input.shape[0], input.shape[1], dims)).cuda()
        input_oh = input_oh.scatter_(-1, input.unsqueeze(-1), 1.)
    else:
        input_oh = torch.zeros((input.shape[0], dims)).cuda()
        input_oh = input_oh.scatter_(-1, input.unsqueeze(-1), 1.)
    return input_oh


def training_phase(step):

    for i in range(1, args['n_epochs'] + 1):
        print("Epoch {} / {}".format(i, args['n_epochs']))

        batch_loss, batch_test_loss = 0, 0
        b_CE_X, b_CE_R, b_CE_N, b_CE_C, b_CE_T, b_CE_V = 0, 0, 0, 0, 0, 0
        t_CE_X, t_CE_R, t_CE_N, t_CE_C, t_CE_T, t_CE_V = 0, 0, 0, 0, 0, 0
        b_l_r, b_l_n, t_l_r, t_l_n = 0, 0, 0, 0
        b_l_adv_r, b_l_adv_n, t_l_adv_r, t_l_adv_n = 0, 0, 0, 0

        for j, x in tqdm(enumerate(train_dl_dist), total=len(train_dl_dist)):

            d, r, n, c, c_r, c_n = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS).cuda()
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS).cuda()
            n_oh = convert_to_one_hot(n, NOTE_DIMS).cuda()

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            step, loss = train(step, d_oh, r_oh, n_oh,
                                d, r, n, c, c_r, c_n, c_r_oh, c_n_oh)
            loss, CE_X, CE_sub, l_r, l_n, l_adv_r, l_adv_n = loss
            batch_loss += loss

            inputs = b_CE_X, b_CE_R
            updates = CE_X, CE_sub
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            b_CE_X, b_CE_R = outputs
            b_l_r += l_r
            b_l_n += l_n
            b_l_adv_r += l_adv_r
            b_l_adv_n += l_adv_n

        for j, x in tqdm(enumerate(val_dl_dist), total=len(val_dl_dist)):
            
            d, r, n, c, c_r, c_n = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS)
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS)
            n_oh = convert_to_one_hot(n, NOTE_DIMS)

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            loss = evaluate(d_oh, r_oh, n_oh,
                            d, r, n, c, c_r, c_n, c_r_oh, c_n_oh)
            loss, CE_X, CE_sub, l_r, l_n, l_adv_r, l_adv_n = loss
            batch_test_loss += loss

            inputs = t_CE_X, t_CE_R
            updates = CE_X, CE_sub
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_CE_X, t_CE_R = outputs
            t_l_r += l_r
            t_l_n += l_n
            t_l_adv_r += l_adv_r
            t_l_adv_n += l_adv_n
        
        print('batch loss: {:.5f}  {:.5f}'.format(batch_loss / len(train_dl_dist),
                                                  batch_test_loss / len(val_dl_dist)))
        print("Data, Rhythm, Note, Chroma")
        print("train loss by term: {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}".format(
            b_CE_X / len(train_dl_dist), b_CE_R / len(train_dl_dist), 
            b_CE_N / len(train_dl_dist),
            b_l_r / len(train_dl_dist), b_l_n / len(train_dl_dist),
            b_l_adv_r / len(train_dl_dist), b_l_adv_n / len(train_dl_dist)
        ))
        print("test loss by term: {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}  {:.5f}".format(
            t_CE_X / len(val_dl_dist), t_CE_R / len(val_dl_dist), 
            t_CE_N / len(val_dl_dist),
            t_l_r / len(val_dl_dist), t_l_n / len(val_dl_dist),
            t_l_adv_r / len(val_dl_dist), t_l_adv_n / len(val_dl_dist)
        ))

    torch.save(model.cpu().state_dict(), save_path)

    timestamp = str(datetime.now())
    save_path_timing = 'params/{}.pt'.format(args['name'] + "_" + timestamp)
    torch.save(model.cpu().state_dict(), save_path_timing)

    if torch.cuda.is_available():
        model.cuda()
    print('Model saved as {}!'.format(save_path))


def evaluation_phase():
    if torch.cuda.is_available():
        model.cuda()

    if os.path.exists(save_path):
        print("Loading {}".format(save_path))
        model.load_state_dict(torch.load(save_path))
    
    def run(dl):
        
        t_CE_X, t_CE_R, t_CE_N, t_CE_C, t_CE_T, t_CE_V = 0, 0, 0, 0, 0, 0
        t_l_r, t_l_n = 0, 0
        t_l_adv_r, t_l_adv_n = 0, 0
        t_acc_x, t_acc_r, t_acc_n, t_acc_t, t_acc_v = 0, 0, 0, 0, 0
        c_acc_r, c_acc_n, c_acc_t, c_acc_v = 0, 0, 0, 0
        data_len = 0

        for i, x in tqdm(enumerate(dl), total=len(dl)):
            d, r, n, c, c_r, c_n = x
            d, r, n, c = d.cuda().long(), r.cuda().long(), \
                         n.cuda().long(), c.cuda().float()
            c_r, c_n, = c_r.cuda().long(), c_n.cuda().long()

            d_oh = convert_to_one_hot(d, EVENT_DIMS)
            r_oh = convert_to_one_hot(r, RHYTHM_DIMS)
            n_oh = convert_to_one_hot(n, NOTE_DIMS)

            c_r_oh = convert_to_one_hot(c_r, 3)
            c_n_oh = convert_to_one_hot(c_n, 3)

            res = model(d_oh, [r_oh, n_oh, c])

            out, dis, z_out, sub_output_lst = res

            # calculate loss
            loss, CE_X, CE_sub = loss_function(out, d,
                                                    sub_output_lst, [r, n],
                                                    dis,
                                                    step,
                                                    beta=args['beta'])

            l_r, l_n = latent_regularized_loss_function(z_out, r, n)
            loss += l_r + l_n

            # adversarial loss
            if is_adversarial:
                l_adv_r, l_adv_n = adversarial_loss(r_r_density, r_n_density, r, n)
                loss += l_adv_r + l_adv_n
            else:
                l_adv_r, l_adv_n = torch.Tensor([0]), torch.Tensor([0])
                

            # update
            inputs = t_CE_X, t_CE_R
            updates = CE_X.item(), CE_sub.item()
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_CE_X, t_CE_R = outputs
            t_l_r += l_r.item()
            t_l_n += l_n.item()
            
                    
            # calculate accuracy
            def acc(a, b, t, trim=False):
                a = torch.argmax(a, dim=-1).squeeze().cpu().detach().numpy()
                b = b.squeeze().cpu().detach().numpy()

                b_acc = 0
                for i in range(len(a)):
                    a_batch = a[i]
                    b_batch = b[i]

                    if trim:
                        b_batch = np.trim_zeros(b_batch)
                        a_batch = a_batch[:len(b_batch)]

                    correct = 0
                    for j in range(len(a_batch)):
                        if a_batch[j] == b_batch[j]:
                            correct += 1
                    acc = correct / len(a_batch)
                    b_acc += acc
                
                return b_acc

            acc_x, acc_r, acc_n = acc(out, d, "d", trim=True), \
                                  acc(sub_output_lst[0], r, "r"), acc(sub_output_lst[1], n, "n")
            data_len += out.shape[0]

            # accuracy update store
            inputs = t_acc_x, t_acc_r, t_acc_n
            updates = acc_x, acc_r, acc_n
            outputs = []
            for idx in range(len(inputs)):
                outputs.append(inputs[idx] + updates[idx])
            t_acc_x, t_acc_r, t_acc_n = outputs
        
        
        # Print results
        print(data_len)
        print("CE: {:.4}  {:.4}  {:.4}".format(t_CE_X / len(dl),
                                                    t_CE_R / len(dl), 
                                                    t_CE_N / len(dl)))
        
        print("Regularized: {:.4}  {:.4}".format(t_l_r / len(dl),
                                                t_l_n / len(dl)))

        print("Adversarial: {:.4}  {:.4}".format(t_l_adv_r / len(dl),
                                                t_l_adv_n / len(dl)))
        
        print("Acc: {:.4}  {:.4}  {:.4}".format(t_acc_x / data_len,
                                                t_acc_r / data_len, 
                                                t_acc_n / data_len))
        
        if is_class:
            print("Class acc: {:.4}  {:.4}".format(c_acc_r / data_len,
                                                    c_acc_n / data_len))

    dl = DataLoader(train_ds_dist, batch_size=128, shuffle=False, num_workers=0)
    run(dl)
    dl = DataLoader(test_ds_dist, batch_size=128, shuffle=False, num_workers=0)
    run(dl)


training_phase(step)
evaluation_phase()
