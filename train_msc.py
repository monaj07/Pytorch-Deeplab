import argparse

import torch
import torch.nn as nn
from torch.utils import data
import numpy as np
import pickle
import cv2
from torch.autograd import Variable
import torch.optim as optim
import scipy.misc
import torch.backends.cudnn as cudnn
import sys
import os
import os.path as osp
import scipy.ndimage as nd
from deeplab.model import Res_Deeplab
from deeplab.loss import CrossEntropy2d
from deeplab.datasets import VOCDataSet
import matplotlib.pyplot as plt
import random
import timeit
start = timeit.timeit()

IMG_MEAN = np.array((104.00698793,116.66876762,122.67891434), dtype=np.float32)

BATCH_SIZE = 1
DATA_DIRECTORY = '../data/VOCdevkit/voc12'
DATA_LIST_PATH = './dataset/list/train_aug.txt'
ITER_SIZE = 10
IGNORE_LABEL = 255
INPUT_SIZE = '321,321'
LEARNING_RATE = 2.5e-4
MOMENTUM = 0.9
NUM_CLASSES = 21
NUM_STEPS = 20000
POWER = 0.9
RANDOM_SEED = 1234
RESTORE_FROM = './dataset/MS_DeepLab_resnet_pretrained_COCO_init.pth'
SAVE_NUM_IMAGES = 2
SAVE_PRED_EVERY = 1000
SNAPSHOT_DIR = './snapshots_msc/'
WEIGHT_DECAY = 0.0005

def get_arguments():
    """Parse all the arguments provided from the CLI.
    
    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="DeepLab-ResNet Network")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Number of images sent to the network in one step.")
    parser.add_argument("--data-dir", type=str, default=DATA_DIRECTORY,
                        help="Path to the directory containing the PASCAL VOC dataset.")
    parser.add_argument("--data-list", type=str, default=DATA_LIST_PATH,
                        help="Path to the file listing the images in the dataset.")
    parser.add_argument("--ignore-label", type=int, default=IGNORE_LABEL,
                        help="The index of the label to ignore during the training.")
    parser.add_argument("--input-size", type=str, default=INPUT_SIZE,
                        help="Comma-separated string with height and width of images.")
    parser.add_argument("--iter-size", type=int, default=ITER_SIZE,
                        help="Number of steps after which gradient update is applied.")
    parser.add_argument("--is-training", action="store_true",
                        help="Whether to updates the running means and variances during the training.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                        help="Base learning rate for training with polynomial decay.")
    parser.add_argument("--momentum", type=float, default=MOMENTUM,
                        help="Momentum component of the optimiser.")
    parser.add_argument("--not-restore-last", action="store_true",
                        help="Whether to not restore last (FC) layers.")
    parser.add_argument("--num-classes", type=int, default=NUM_CLASSES,
                        help="Number of classes to predict (including background).")
    parser.add_argument("--num-steps", type=int, default=NUM_STEPS,
                        help="Number of training steps.")
    parser.add_argument("--power", type=float, default=POWER,
                        help="Decay parameter to compute the learning rate.")
    parser.add_argument("--random-mirror", action="store_true",
                        help="Whether to randomly mirror the inputs during the training.")
    parser.add_argument("--random-scale", action="store_true",
                        help="Whether to randomly scale the inputs during the training.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                        help="Random seed to have reproducible results.")
    parser.add_argument("--restore-from", type=str, default=RESTORE_FROM,
                        help="Where restore model parameters from.")
    parser.add_argument("--save-num-images", type=int, default=SAVE_NUM_IMAGES,
                        help="How many images to save.")
    parser.add_argument("--save-pred-every", type=int, default=SAVE_PRED_EVERY,
                        help="Save summaries and checkpoint every often.")
    parser.add_argument("--snapshot-dir", type=str, default=SNAPSHOT_DIR,
                        help="Where to save snapshots of the model.")
    parser.add_argument("--weight-decay", type=float, default=WEIGHT_DECAY,
                        help="Regularisation parameter for L2-loss.")
    parser.add_argument("--gpu", type=int, default=0,
                        help="choose gpu device.")
    return parser.parse_args()

args = get_arguments()

def loss_calc(pred, label, gpu):
    """
    This function returns cross entropy loss for semantic segmentation
    """
    # out shape batch_size x channels x h x w -> batch_size x channels x h x w
    # label shape h x w x 1 x batch_size  -> batch_size x 1 x h x w
    label = torch.from_numpy(label).long()
    label = Variable(label).cuda(gpu)
    m = nn.LogSoftmax()
    criterion = CrossEntropy2d().cuda(gpu)
    pred = m(pred)
    
    return criterion(pred, label)


def lr_poly(base_lr, iter, max_iter, power):
    return base_lr*((1-float(iter)/max_iter)**(power))


def get_1x_lr_params_NOscale(model):
    """
    This generator returns all the parameters of the net except for 
    the last classification layer. Note that for each batchnorm layer, 
    requires_grad is set to False in deeplab_resnet.py, therefore this function does not return 
    any batchnorm parameter
    """
    b = []

    b.append(model.conv1)
    b.append(model.bn1)
    b.append(model.layer1)
    b.append(model.layer2)
    b.append(model.layer3)
    b.append(model.layer4)

    
    for i in range(len(b)):
        for j in b[i].modules():
            jj = 0
            for k in j.parameters():
                jj+=1
                if k.requires_grad:
                    yield k

def get_10x_lr_params(model):
    """
    This generator returns all the parameters for the last layer of the net,
    which does the classification of pixel into classes
    """
    b = []
    b.append(model.layer5.parameters())

    for j in range(len(b)):
        for i in b[j]:
            yield i
            
            
def adjust_learning_rate(optimizer, i_iter):
    """Sets the learning rate to the initial LR divided by 5 at 60th, 120th and 160th epochs"""
    lr = lr_poly(args.learning_rate, i_iter, args.num_steps, args.power)
    optimizer.param_groups[0]['lr'] = lr
    optimizer.param_groups[1]['lr'] = lr * 10


def main():
    """Create the model and start the training."""
    
    h, w = map(int, args.input_size.split(','))
    input_size = (h, w)

    cudnn.enabled = False
    gpu = args.gpu

    # Create network.
    model = Res_Deeplab(num_classes=args.num_classes)
    # For a small batch size, it is better to keep 
    # the statistics of the BN layers (running means and variances)
    # frozen, and to not update the values provided by the pre-trained model. 
    # If is_training=True, the statistics will be updated during the training.
    # Note that is_training=False still updates BN parameters gamma (scale) and beta (offset)
    # if they are presented in var_list of the optimiser definition.

    saved_state_dict = torch.load(args.restore_from)
    new_params = model.state_dict().copy()
    for i in saved_state_dict:
        #Scale.layer5.conv2d_list.3.weight
        i_parts = i.split('.')
        # print i_parts
        if not args.num_classes == 21 or not i_parts[1]=='layer5':
            new_params['.'.join(i_parts[1:])] = saved_state_dict[i]
    model.load_state_dict(new_params)
    #model.float()
    #model.eval() # use_global_stats = True
    model.train()
    model.cuda(args.gpu)
    
    cudnn.benchmark = True

    if not os.path.exists(args.snapshot_dir):
        os.makedirs(args.snapshot_dir)


    trainloader = data.DataLoader(VOCDataSet(args.data_dir, args.data_list, max_iters=args.num_steps*args.iter_size,
                    crop_size=input_size, scale=args.random_scale, mirror=args.random_mirror, mean=IMG_MEAN), 
                    batch_size=args.batch_size, shuffle=True, num_workers=1, pin_memory=True)

    optimizer = optim.SGD([{'params': get_1x_lr_params_NOscale(model), 'lr': args.learning_rate }, 
                {'params': get_10x_lr_params(model), 'lr': 10*args.learning_rate}], 
                lr=args.learning_rate, momentum=args.momentum,weight_decay=args.weight_decay)
    optimizer.zero_grad()

    b_loss = 0
    for i_iter, batch in enumerate(trainloader):

        images, labels, _, _ = batch
        images, labels = Variable(images), labels.numpy()
        h, w = images.size()[2:]
        images075 = nn.Upsample(size=(int(h*0.75), int(w*0.75)), mode='bilinear')(images)
        images05 = nn.Upsample(size=(int(h*0.5), int(w*0.5)), mode='bilinear')(images)

        out = model(images.cuda(args.gpu))
        out075 = model(images075.cuda(args.gpu))
        out05 = model(images05.cuda(args.gpu))
        o_h, o_w = out.size()[2:]
        interpo1 = nn.Upsample(size=(o_h, o_w), mode='bilinear')
        interpo2 = nn.Upsample(size=(h, w), mode='bilinear')
        out_max = interpo2(torch.max(torch.stack([out, interpo1(out075), interpo1(out05)]), dim=0)[0])

        loss = loss_calc(out_max, labels, args.gpu)
        d1, d2 = float(labels.shape[1]), float(labels.shape[2])
        loss100 = loss_calc(out, nd.zoom(labels, (1.0, out.size()[2]/d1, out.size()[3]/d2), order=0), args.gpu)
        loss075 = loss_calc(out075, nd.zoom(labels, (1.0, out075.size()[2]/d1, out075.size()[3]/d2), order=0), args.gpu)
        loss05 = loss_calc(out05, nd.zoom(labels, (1.0, out05.size()[2]/d1, out05.size()[3]/d2), order=0), args.gpu)
        loss_all = (loss + loss100 + loss075 + loss05) / args.iter_size
        loss_all.backward()
        b_loss += loss_all.data.cpu().numpy()

        b_iter = i_iter / args.iter_size

        if b_iter >= args.num_steps-1:
            print 'save model ...'
            optimizer.step()
            torch.save(model.state_dict(),osp.join(args.snapshot_dir, 'VOC12_scenes_'+str(args.num_steps)+'.pth'))
            break

        if i_iter % args.iter_size == 0 and i_iter != 0:
            print 'iter = ', b_iter, 'of', args.num_steps,'completed, loss = ', b_loss
            optimizer.step()
            adjust_learning_rate(optimizer, b_iter)
            optimizer.zero_grad()
            b_loss = 0

        if b_iter % args.save_pred_every == 0 and b_iter!=0:
            print 'taking snapshot ...'
            torch.save(model.state_dict(),osp.join(args.snapshot_dir, 'VOC12_scenes_'+str(b_iter)+'.pth'))

    end = timeit.timeit()
    print end-start,'seconds'

if __name__ == '__main__':
    main()
