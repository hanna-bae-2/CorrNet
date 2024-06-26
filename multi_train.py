import argparse
import os 
import torch
import torch.distributed as dist 
import torch.multiprocessing as mp 
import numpy as np 
import random 
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader 
from torchvision import transforms 
import tensorboard as to 
import tensorflow as tf 
from torch import optim 
import pandas as pd
import uuid 

# Internal modules 
from model.dataset import MSCOCO
from model.corrnet import CorrNet 

# Conflict between PyTorch and Tensorflow 
tf.io.gfile = tb.compat.tensorflow_stub.io.gfile
def test(net, data, temperature, device=None):
    # Evaluation mode
    net.eval()

    # Current validation loss
    total_loss, total_num = 0.0, 0.0

    # Evaluate the network
    with torch.no_grad():
        for x_1, x_2, xn_1, xn_2, xn_idx in data:
            # Get sample
            x_1, x_2 = x_1.to(device), x_2.to(device)

            # Compute Z
            h_1, z_1 = net(x_1)
            h_2, z_2 = net(x_2)
            z = torch.cat([z_1, z_2], dim=0)

            # Similarity matrix
            sim_matrix = torch.exp(torch.mm(z, z.t().contiguous()) / temperature)
            mask = (torch.ones_like(sim_matrix) - torch.eye(z.shape[0], device=sim_matrix.device)).bool()
            sim_matrix = torch.sum(sim_matrix.masked_select(mask).view(z.shape[0], -1), dim=-1)

            # Similarity matrix of positive pairs
            pos_sim = torch.exp(torch.sum(z_1 * z_2, dim=-1) / temperature)
            pos_sim = torch.cat([pos_sim, pos_sim], dim=0)

            # Compute loss
            loss = (- torch.log(pos_sim / sim_matrix)).mean()

            # Add to running loss
            total_num += z.shape[0]
            total_loss += loss.item() * z.shape[0]

        return total_loss / total_num


def train(net, data, otp, temperature, device, gradient_accumulation, interval, full_path):
    # Set net to the train mode
    net.train()

    # Current loss
    total_loss, total_num = 0.0, 0

    gradient_accumulation_counter = 0
    otp.zero_grad()
    training_updates = 0

    # Train on batch
    for x_1, x_2, xn_1, xn_2, xn_idx in data:
        # Prepare batch
        x_1 = torch.cat([x_1, xn_1[xn_idx]], dim=0)
        x_2 = torch.cat([x_2, xn_2[xn_idx]], dim=0)

        x_1, x_2 = x_1.to(device), x_2.to(device)

        # Compute Z
        h_1, z_1 = net(x_1)
        h_2, z_2 = net(x_2)
        z = torch.cat([z_1, z_2], dim=0)

        # Similarity matrix
        sim_matrix = torch.exp(torch.mm(z, z.t().contiguous()) / temperature)
        mask = (torch.ones_like(sim_matrix) - torch.eye(z.shape[0], device=sim_matrix.device)).bool()
        sim_matrix = torch.sum(sim_matrix.masked_select(mask).view(z.shape[0], -1), dim=-1)

        # Similarity matrix of positive pairs
        pos_sim = torch.exp(torch.sum(z_1 * z_2, dim=-1) / temperature)
        pos_sim = torch.cat([pos_sim, pos_sim], dim=0)

        # Compute loss
        loss = (- torch.log(pos_sim / sim_matrix)).mean()

        # Backward
        loss.backward()
        gradient_accumulation_counter += 1

        # Check whether to optimize based on gradients accumulation
        if gradient_accumulation_counter >= gradient_accumulation:
            # Optimize
            otp.step()

            # Set grads to zero
            otp.zero_grad()

            # Set gradient accumulation to zero
            gradient_accumulation_counter = 0

            # Save partial cornet
            training_updates += 1

            if (training_updates % interval) == 0:
                print('Partial loss on {}: {}'.format(training_updates, total_loss / total_num))
                torch.save(net.state_dict(), os.path.join(full_path, 'cornet_partial.pth'))

        # Add to current loss
        total_num += z.shape[0]
        total_loss += loss.item() * z.shape[0]

    # Check residual gradients
    if gradient_accumulation_counter > 0:
        # Optimize
        otp.step()

    return total_loss / total_num
def training_loop(max_epoch, net, training, validation, opt, temperature, results,
                  best_val_loss, path_experiment, writer, is_fine_tuning, val_interval, device, gradient_accumulation, train_interval):
    loss_val = best_val_loss

    for epoch in range(1, max_epoch + 1):
        print('Epoch: {} --- {}'.format(epoch, max_epoch))

        # Train
        loss_train = train(net, training, opt, temperature, device, gradient_accumulation, train_interval, path_experiment)
        results['loss_train'].append(loss_train)

        # Validate
        print('Validation check: {} ({})'.format(epoch % val_interval, epoch % val_interval == 0))
        if (epoch % val_interval) == 0:
            loss_val = test(net, validation, temperature, device)
            results['loss_val'].append(loss_val)

            # Log losses on tensorboard
            writer.add_scalar('Training (FT)' if is_fine_tuning else 'Training', loss_train, epoch)
            writer.add_scalar('Validation (FT)' if is_fine_tuning else 'Validation', loss_val, epoch)

            # Save statistics
            data_frame = pd.DataFrame(data=results['loss_train'], index=range(1, len(results['loss_train']) + 1))
            data_frame.to_csv(os.path.join(path_experiment, 'results_train.csv'), index_label='epoch')
            data_frame = pd.DataFrame(data=results['loss_val'], index=range(1, len(results['loss_val']) + 1))
            data_frame.to_csv(os.path.join(path_experiment, 'results_val.csv'), index_label='epoch ({})'.format(val_interval))

            # Save best network
            if loss_val <= best_val_loss:
                best_val_loss = loss_val
                torch.save(net.state_dict(), os.path.join(path_experiment, 'cornet_val.pth'))
            torch.save(net.state_dict(), os.path.join(path_experiment, 'cornet_{}.pth'.format(epoch)))

        print('Epoch: {:d} - {:d} / Train: {:.4f} / Val: {:.4f}'.format(epoch, max_epoch, loss_train, loss_val))

    return best_val_loss

def set_random_seeds(random_seed = 0):
    torch.manual_seed(random_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(random_seed)
    random.seed(random_seed)

def init_for_distributed(opts):
    # 1. setting for distributed training
    opts.global_rank = int(os.environ['RANK'])
    opts.local_rank = int(os.environ['LOCAL_RANK'])
    opts.world_size = int(os.environ['WORLD_SIZE'])
    torch.cuda.set_device(opts.local_rank)
    if opts.global_rank is not None and opts.local_rank is not None:
        print('Use GPU: [{}/{}] for training'.format(opts.global_rank, opts.local_rank))

    # 2. init_process_group
    dist.init_process_group(backend = 'nccl')
    # if put this function, the all processes block at all 
    torch.distributed.barrier() 
    return 


def main(opts):
    # 1. set random seeds 
    set_random_seeds(random_seed = 0)

    # 2. initialization 
    init_for_distributed(opts)

    # 3. visdom 
    vis = None
    path_to_running_experiment = os.path.join(opts.experiment_folder, opts.name_experiment)
    print('Starting experiment: ', path_to_running_experiment)

    # Check singularity
    if os.path.exists(path_to_running_experiment):
        raise RuntimeError('There is an experiment with the same name. Check whether the hype-parameters are the same or try again.')
    else:
        os.makedirs(path_to_running_experiment)

    # Device
    if device < 0:
        print('Experiments running on CPU.')
        device = torch.device('cpu')
    else:
        print('Experiments running on CUDA: {}'.format(device))
        device = torch.device('cuda:{}'.format(device))

    # Initialize tensorboard writer
    writer = SummaryWriter(path_to_running_experiment)

    # Transforms for training
    transform_train = transforms.Compose([
        transforms.ColorJitter(brightness=(.1, 1.2), contrast=(0.8, 1.4), saturation=(0.8, 1.2), hue=0.1),
        transforms.RandomPerspective(distortion_scale=0.5, p=0.5),
        transforms.Resize((120, 160)),
        transforms.ToTensor()
    ])

    # Transforms for Validation
    transform_val = transforms.Compose([transforms.Resize((120, 160)), transforms.ToTensor()])

    # Load dataset
    data_train = MSCOCO(os.path.join(opts.dataset_folder, 'train2014'), transform_train, in_image_sampling_min_crop_size=.8, in_image_sampling_likelihood=0.0)
    data_val = MSCOCO(os.path.join(opts.dataset_folder, 'val2014'), transform_val, max_loaded_samples=5000)

    # Load data loader
    loader_train = DataLoader(data_train, batch_size=opts.batch_size, shuffle=True, num_workers=1, drop_last=True)
    loader_val = DataLoader(data_val, batch_size=opts.batch_size, shuffle=False, num_workers=1, drop_last=False)
    # pretrained model preprocessing 
    #weights = torch.load(pre_trained, map_location=device)
    
    # Initialize the network
    cornet = CorrNet(feature_dim=opts.description_size)
    cornet = cornet.to(device)
    cornet.train()

    # Initialize variables
    results = {'loss_train': [], 'loss_val': []}
    best_val_loss = 1000000.0
    val_interval = 1
    train_interval = 512

    # Fine-tuning
    if opts.training_mode > 1:
        # Initialize optimizer for fine-tuning
        optimizer = optim.Adam(cornet.f.parameters(), lr=opts.learning_rate_ft, weight_decay=opts.weight_decay_ft)

        # Fine-tuning Training loop
        best_val_loss = training_loop(opts.epochs_ft, cornet, loader_train, loader_val, optimizer, opts.temperature, results,
                                      best_val_loss, path_to_running_experiment, writer, True, val_interval, device, opts.gradient_accumulation, train_interval)

    # Train the whole network
    if opts.training_mode < 3:
        # Initialize optimizer
        optimizer = optim.Adam(cornet.parameters(), lr=opts.learning_rate, weight_decay=opts.weight_decay)

        # Training loop
        training_loop(opts.epochs, cornet, loader_train, loader_val, optimizer, opts.temperature, results, best_val_loss,
                      path_to_running_experiment, writer, False, val_interval, device, opts.gradient_accumulation, train_interval)

    # Close tensorboard writer
    writer.close()
   



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train CorrNet on MS-COCO 2014 for correspondence detection.')
    parser.add_argument('--local_rank', type=int, help='Local rank. Necessary for using the torch.distributed.launch utility.')
    parser.add_argument('--global_rank', type=int, default=0)
    parser.add_argument('--num_workers', type=int, default=24)
    parser.add_argument('--dataset_folder', default='./ms_coco_2014/', type=str, help='Dataset folder.')
    parser.add_argument('--experiment_folder', default='./results/', type=str, help='Experiment folder.')
    parser.add_argument('--pre_trained', default='./simclr.pth', type=str, help='Trained global CorNet.')
    parser.add_argument('--description_size', default=128, type=int, help='Feature dimension of the latent vector.')
    parser.add_argument('--temperature', default=0.5, type=float, help='Temperature of the loss function.')
    parser.add_argument('--epochs', default=500, type=int, help='Number of epochs.')
    parser.add_argument('--batch_size', default=1, type=int, help='Batch size.')
    parser.add_argument('--learning_rate', default=1e-3, type=float, help='Learning rate.')
    parser.add_argument('--weight_decay', default=1e-6, type=float, help='Weight decay.')
    parser.add_argument('--training_mode', default=1, type=int, help='Training mode: 1 - train CorNet, 2 - fine-tune CorNet.f first, or 3 - fine-tune f(.) only.')
    parser.add_argument('--epochs_ft', default=500, type=int, help='Number of epochs for fine-tuning.')
    parser.add_argument('--learning_rate_ft', default=1e-3, type=float, help='Learning rate for fine-tuning.')
    parser.add_argument('--weight_decay_ft', default=1e-6, type=float, help='Weight decay for fine-tuning.')
    parser.add_argument('--cuda', default=-1, type=int, help='CUDA id. The default running device is CPU.')
    parser.add_argument('--gradients_accumulation', default=1, type=int, help='Number of gradients accumulation iterations.')


    opts = parser.parse_args()

    try:
        main(opts)
    except RuntimeError as e:
        print(e)

    exit(0)