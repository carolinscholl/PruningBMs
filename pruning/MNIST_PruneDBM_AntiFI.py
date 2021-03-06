import warnings
warnings.filterwarnings("ignore")

import os
import sys
import env
import tensorflow as tf
import numpy as np
import pickle
from bm.dbm import DBM
from bm.rbm.rbm import BernoulliRBM, logit_mean
from bm.init_BMs import * # helper functions to initialize, fit and load RBMs and 2 layer DBM
from bm.utils.dataset import *
from bm.utils import *
from rbm_utils.stutils import *
from rbm_utils.fimdiag import * # functions to compute the diagonal of the FIM for RBMs
from copy import deepcopy
import argparse
import random
from shutil import copy
import pathlib
from sklearn.metrics import accuracy_score
from sklearn.linear_model import LogisticRegression
from sklearn.externals import joblib
from pruning.MNIST_Baselines import *

random.seed(42)
np.random.seed(42)

# if machine has multiple GPUs only use first one
#os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
#os.environ["CUDA_VISIBLE_DEVICES"]="0"

class Struct:
    def __init__(self, **entries):
        self.__dict__.update(entries)

def main(perc=10, n_sessions=10):

    # check that we have access to a GPU and that we only use one!
    if tf.test.gpu_device_name():
        print('Default GPU Device: {}'.format(tf.test.gpu_device_name()))
        from tensorflow.python.client import device_lib
        print(device_lib.list_local_devices())
        print("Workspace initialized")
    else:
        print("Consider installing GPU version of TF and running sampling from DBMs on GPU.")

    if 'session' in locals() and tf.compat.v1.Session() is not None:
        print('Close interactive session')
        tf.compat.v1.Session().session.close()

    logreg_digits = get_classifier_trained_on_raw_digits()

    # Load MNIST
    print("\nPreparing data ...\n\n")
    train, test = preprocess_MNIST()
    bin_X_train = train[0]
    y_train = train[1]
    bin_X_test = test[0]
    y_test = test[1]

    n_train = len(bin_X_train)

    # path to where models shall be saved
    model_path = os.path.join('..', 'models', 'MNIST', f'antiFI_{perc}perc_{n_sessions}sessions')
    res_path = os.path.join(model_path,'res')

    assert not os.path.exists(model_path), "model path already exists - abort"
    os.makedirs(res_path)

    path = pathlib.Path(__file__).absolute() # save the script
    copy(path, res_path+'/script.py')

    # LOAD MODEL
    args = get_initial_args()
    dbm = get_initial_DBM()
    rbm1 = load_rbm1(Struct(**args))
    rbm2 = load_rbm2(Struct(**args))

    weights = dbm.get_tf_params(scope='weights')
    W1 = weights['W']
    hb1 = weights['hb']
    W2 = weights['W_1']
    hb2 = weights['hb_1']
    vb = weights['vb']

    # adjust parameters of rbm2 to the current ones:
    rbm2.set_params(initialized_=False)
    rbm2.set_params(vb_init=hb1)
    rbm2.set_params(hb_init=hb2)
    rbm2.set_params(W_init=W2)
    rbm2.init()

    masks = dbm.get_tf_params(scope='masks')
    rf_mask1 = masks['rf_mask']
    prune_mask1 = masks['prune_mask']
    rf_mask2 = masks['rf_mask_1']
    prune_mask2 = masks['prune_mask_1']

    # Variance (true) or heuristic estimate (false)?
    USE_VAR = True

    # Delete all with an FI of zero (false) or constantly x percent of weights (true)?
    CONSTANTLY = False

    # Anti-Fi pruning?
    ANTI_FI = True

    if USE_VAR:
        print("Pruning based on variance estimate of FIM diagonal.")
    else:
        print("Pruning based on heuristic estimate")

    THR = perc/100 #threshold for percentile
    if ANTI_FI:
        THR = round(1-THR,1)

    # PREPARE PRUNING
    n_iter = n_sessions
    n_check = 2
    pruning_session = 0

    nv = args['n_vis']
    nh1 = args['n_hidden'][0]
    nh2 = args['n_hidden'][1]

    # compute FI before first pruning
    SAMPLE_EVERY = 200
    samples = dbm.sample_gibbs(n_gibbs_steps=SAMPLE_EVERY, save_model=False, n_runs=n_train)

    s_v = samples[:,:nv]
    s_h1 = samples[:,nv:nv+nh1]
    s_h2 = samples[:,nv+nh1:]

    temp_mask1 = rf_mask1 * prune_mask1
    samples = np.hstack((s_v, s_h1))
    var_est1, heu_est1 = FI_weights_var_heur_estimates(samples, nv, nh1, W1) # call without mask

    if USE_VAR:
        fi_weights_after_joint_RBM1 = var_est1.reshape((nh1,nv)).T * temp_mask1
    else:
        fi_weights_after_joint_RBM1 = heu_est1.reshape((nh1,nv)).T * temp_mask1

    temp_mask2 = rf_mask2 * prune_mask2
    samples = np.hstack((s_h1, s_h2))
    var_est2, heu_est2 = FI_weights_var_heur_estimates(samples, nh1, nh2, W2) # call without mask
    if USE_VAR:
        fi_weights_after_joint_RBM2 = var_est2.reshape((nh2,nh1)).T * temp_mask2
    else:
        fi_weights_after_joint_RBM2 = heu_est2.reshape((nh2,nh1)).T * temp_mask2

    fi_weights2=fi_weights_after_joint_RBM2

    if CONSTANTLY:
        left = int(len(W1[np.where(rf_mask1!=0)].flatten()) + len(hb1)*len(hb2)) # number of units left
        list_n_to_remove = []
        for i in range(n_iter):
            list_n_to_remove.append(int(THR*left))
            left = int(left - list_n_to_remove[i])
        np.save(os.path.join(res_path, 'n_to_remove.npy'), np.array(list_n_to_remove))

    # create result arrays
    res_acc_logreg = np.zeros((n_iter, n_check)) # accuracy of log reg
    res_act_weights_L2 = np.zeros((n_iter, n_check)) # active weights layer 2
    res_act_weights_L1 = np.zeros((n_iter, n_check)) # active weights layer 1
    res_n_hid_L2 = np.zeros((n_iter, n_check)) # number hidden units layer 2
    res_n_hid_L1 = np.zeros((n_iter, n_check))

    def save_results():
        np.save(os.path.join(res_path, 'AccLogReg.npy'), res_acc_logreg)
        np.save(os.path.join(res_path, 'n_active_weights_L2.npy'), res_act_weights_L2)
        np.save(os.path.join(res_path, 'n_active_weights_L1.npy'), res_act_weights_L1)
        np.save(os.path.join(res_path, 'n_hid_units_L2.npy'), res_n_hid_L2)
        np.save(os.path.join(res_path, 'n_hid_units_L1.npy'), res_n_hid_L1)

    save_results()

    # retrain the DBMs for just 10 epochs instead of 20
    args['epochs'] = (20, 20, 10)
    args['max_epoch'] = 10

    active_nh1 = nh1

    for it in range(n_iter):                   

        pruning_session += 1

        print("\n################## Pruning session", pruning_session, "##################")
        print("################## Start pruning both layers based on the same samples ##################")

        print("################## Start pruning first layer ##################")

        fi_weights1 = fi_weights_after_joint_RBM1.reshape((nv,nh1)) # already computed FI above
        new_weights = deepcopy(W1)

        temp_mask = rf_mask1 * prune_mask1

        perc = np.percentile(fi_weights1[np.where(temp_mask!=0)],THR*100)

        if not CONSTANTLY:

            print("Prune",round(1-THR,1), " percentile of weights with highest FI")

            n_pruned = sum(fi_weights1[np.where(temp_mask!=0)].flatten()>perc)
            print(n_pruned, "weights of a total of",
                len(W1[np.where(temp_mask!=0)].flatten()), "are pruned: ",
                n_pruned/len(W1[np.where(temp_mask!=0)].flatten()), "of all weights.")
            print("Weights with FI higher than", perc, "pruned")

            keep = np.reshape(fi_weights1, (nv, nh1)) <= perc

        if CONSTANTLY:
            n_pruned = sum(fi_weights1[np.where(temp_mask!=0)].flatten()>perc)
            if (n_pruned/len(W1[np.where(temp_mask!=0)].flatten())) < round(1-THR,1):
                print(THR, "th percentile = 0, randomly select some more in order to prune ", round(1-THR,1), "of weights.")
                # make a copy of the array
                copy_fi = deepcopy(fi_weights1)

                # indicate indices that are pruned
                copy_fi[temp_mask.astype(bool)==0]=-100
                copy_fi = copy_fi.flatten()

                indices_of_all_remaining = np.where(copy_fi!= -100)
                indices_of_all_remaining = np.squeeze(np.asarray(indices_of_all_remaining))
                indices_of_all_remaining = set(indices_of_all_remaining)

                indices_of_important_ones = np.where(copy_fi > perc)
                indices_of_important_ones = np.squeeze(np.asarray(indices_of_important_ones))
                indices_of_important_ones = set(indices_of_important_ones)

                # that many we have to remove in order to delete 10%:
                #n_10_percent = int(round(1-THR,1)*sum(temp_mask.flatten()!=0).flatten())
                n_10_percent = list_n_to_remove[it]

                # subtract the ones we prune because they have an FI > 0
                n_to_remove = n_10_percent - len(indices_of_important_ones)

                # get subset of indices where we have to select from
                indices_of_all_remaining = indices_of_all_remaining - indices_of_important_ones

                # randomly select 10% of all indices where FI is 0
                randomly_selected_ones = random.sample(indices_of_all_remaining, n_to_remove)

                keep = temp_mask.flatten()
                keep[(list(indices_of_important_ones))] = False
                keep[(list(randomly_selected_ones))] = False # set them to false
                keep = keep.reshape(nv, nh1)
            else:
                print("Prune",round(1-THR,1), " percentile of weights with highest FI")

                perc = np.percentile(fi_weights1[np.where(temp_mask!=0)],THR*100)
                print("Weights with FI higher than", perc, "pruned")

                print(n_pruned, "weights of a total of",
                len(W1[np.where(temp_mask!=0)].flatten()), "are pruned: ",
                n_pruned/len(W1[np.where(temp_mask!=0)].flatten()), "of all weights.")

                keep = np.reshape(fi_weights1, (nv, nh1)) <= perc

        keep[rf_mask1==0]=0 # these weights don't exist anyways
        keep[prune_mask1==0]=0

        # check how many hidden units in last layer are still connected
        no_left_hidden = 0 # number of hidden units left
        indices_of_left_hiddens = [] # indices of the ones we keep
        for i in range(nh1):
            if sum(keep[:,i]!=0):
                no_left_hidden+=1
                indices_of_left_hiddens.append(i)
        print(no_left_hidden, "hidden units in first layer are still connected by weights to visibles.",nh1-no_left_hidden, "unconnected hidden units are removed.")

        no_left_visible = 0
        indices_of_left_visibles = []
        indices_of_lost_visibles = []
        for i in range(nv):
            if sum(keep[i]!=0):
                no_left_visible+=1
                indices_of_left_visibles.append(i)
            else:
                indices_of_lost_visibles.append(i)
        print(no_left_visible, "visible units are still connected by weights.", nv-no_left_visible, "unconnected visible units.")

        print("Indices of lost visibles:", indices_of_lost_visibles)


        keep1 = keep # save mask
        new_weights1 = new_weights # save weights

        # cannot initialise RBM1 yet because perhaps hidden units of first layer will be removed as a consequence of weight pruning in second layer
        # (we remove all unconnected hiddens, even if connected from just one side)

        # set number of hidden units in second layer!
        # for next layer the hiddens are the visibles
        indices_of_left_intermediates = indices_of_left_hiddens
        no_left_intermediates = no_left_hidden

        ############## PRUNE 1 #######################
        # prune second layer
        print("\nStart pruning second layer")


        new_weights = deepcopy(W2)
        fi_weights2 = fi_weights2.reshape((nh1,nh2))

        temp_mask = rf_mask2 * prune_mask2

        perc = np.percentile(fi_weights2[np.where(temp_mask!=0)],THR*100)

        if not CONSTANTLY:

            print("Prune",round(1-THR,1), " percentile of weights with highest FI")

            n_pruned = sum(fi_weights2[np.where(temp_mask!=0)].flatten()>perc)
            print(n_pruned, "weights of a total of",
                len(W2[np.where(temp_mask!=0)].flatten()), "are pruned: ",
                n_pruned/len(W2[np.where(temp_mask!=0)].flatten()), "of all weights.")
            print("Weights with FI higher than", perc, "pruned")

            keep = np.reshape(fi_weights2, (nh1, nh2)) <= perc

        if CONSTANTLY:
            n_pruned = sum(fi_weights2[np.where(temp_mask!=0)].flatten()>perc)
            if (n_pruned/len(W2[np.where(temp_mask!=0)].flatten())) < round(1-THR,1):
                print(perc, "th percentile = 0, randomly select some more in order to prune ", round(1-THR,1), "of weights.")
                # make a copy of the array
                copy_fi = deepcopy(fi_weights2)

                # indicate indices that are pruned
                copy_fi[temp_mask.astype(bool)==0]=-100
                copy_fi = copy_fi.flatten()

                indices_of_all_remaining = np.where(copy_fi!= -100)
                indices_of_all_remaining = np.squeeze(np.asarray(indices_of_all_remaining))
                indices_of_all_remaining = set(indices_of_all_remaining)

                indices_of_important_ones = np.where(copy_fi > perc)
                indices_of_important_ones = np.squeeze(np.asarray(indices_of_important_ones))
                indices_of_important_ones = set(indices_of_important_ones)

                # that many we have to remove in order to delete 10%:
                n_10_percent = int(round(1-THR,1)*sum(temp_mask.flatten()!=0).flatten())

                # subtract the ones we prune because they have an FI > 0
                n_to_remove = n_10_percent - len(indices_of_important_ones)

                # get subset of indices where we have to select from
                indices_of_all_remaining = indices_of_all_remaining - indices_of_important_ones

                # randomly select 10% of all indices where FI is 0
                randomly_selected_ones = random.sample(indices_of_all_remaining, n_to_remove)

                keep = temp_mask.flatten()
                keep[(list(indices_of_important_ones))] = False
                keep[(list(randomly_selected_ones))] = False # set them to false
                keep = keep.reshape(nh1, nh2)

            else:
                print("Prune",round(1-THR,1), " percentile of weights with highest FI")

                print("Weights with FI higher than", perc, "pruned")

                print(n_pruned, "weights of a total of",
                len(W2[np.where(temp_mask!=0)].flatten()), "are pruned: ",
                n_pruned/len(W2[np.where(temp_mask!=0)].flatten()), "of all weights.")


                keep = np.reshape(fi_weights2, (nh1, nh2)) <= perc

        keep[rf_mask2==0]=0 # these weights don't exist anyways
        keep[prune_mask2==0]=0

        # check how many hidden units in last layer are still connected
        no_left_hidden = 0 # number of hidden units left
        indices_of_left_hiddens = [] # indices of the ones we keep
        for i in range(nh2):
            if sum(keep[:,i]!=0):
                no_left_hidden+=1
                indices_of_left_hiddens.append(i)
        print(no_left_hidden, "hidden units in last layer are still connected by weights.", nh2-no_left_hidden, "unconnected hidden units are removed.")

        no_left_visible_rbm2 = 0
        indices_of_left_visibles_rbm2 = []
        indices_of_lost_visibles_rbm2 = []
        for i in range(nh1):
            if sum(keep[i]!=0):
                no_left_visible_rbm2+=1
                indices_of_left_visibles_rbm2.append(i)
            else:
                indices_of_lost_visibles_rbm2.append(i)

        # only keep the ones that still have connections to both their neighboring layers, otherwise they are lost bc they are dead ends
        indices_of_left_intermediate_units=np.intersect1d(indices_of_left_visibles_rbm2, indices_of_left_intermediates)
        nh1 = len(indices_of_left_intermediate_units)
        print(nh1, "hidden units in intermediate layer are still connected by weights to both layers.", no_left_intermediates-nh1, "are removed.")

        # store mask and weights
        keep2 = keep
        new_weights2 = new_weights

        # INITIALISE NEW RBMs and DBM

        # now we initialise the new first layer (RBM1)
        # get visible and hidden biases
        vb = deepcopy(vb)
        hb = deepcopy(hb1)
        hb = hb[indices_of_left_intermediate_units]

        # adjust hidden biases -> gave bad performance
        #mean_act_per_neuron = np.mean(s_h1, axis = 0)
        #hb = (-1/(1+np.exp(mean_act_per_neuron)))
        #hb=hb[indices_of_left_intermediate_units]

        # set new weights
        new_weights1[keep1==0]=0
        new_weights1 = new_weights1[:, indices_of_left_intermediate_units]
        keep1 = keep1[:, indices_of_left_intermediate_units]
        new_weights1[keep1==0]=0

        args['rbm1_dirpath'] = os.path.join(model_path,'MNIST_PrunedRBM1_both_Sess{}/'.format(pruning_session))
        args['vb_init']=(vb, -1)
        args['hb_init']=(hb, -2)
        args['prune']=True
        args['n_hidden'] = (nh1, 676)
        args['freeze_weights']=keep1
        args['w_init']=(new_weights1, 0.1, 0.1)
        args['n_vis']=nv
        args['filter_shape']=[(20,20)] # deactivate receptive fields, they are now realised over the prune_mask (keep)!!!!!!

        print("Shape of new weights of RBM1", new_weights1.shape)

        rbm1_pruned = init_rbm1(Struct(**args))

        # initialise second layer (RBM2)
        # set number of hidden units
        nh2 = no_left_hidden

        # get visible and hidden biases
        hb = deepcopy(hb2)
        hb=hb[indices_of_left_hiddens]
        vb = deepcopy(hb1)
        vb=vb[indices_of_left_intermediate_units]

        # set new weights
        new_weights2[keep2==0]=0
        new_weights2 = new_weights2[:, indices_of_left_hiddens]
        new_weights2 = new_weights2[indices_of_left_intermediate_units, :]
        keep2 = keep2[:, indices_of_left_hiddens]
        keep2 = keep2[indices_of_left_intermediate_units, :]

        # set params for new RBM2
        args['rbm2_dirpath'] = os.path.join(model_path,'MNIST_PrunedRBM2_both_Sess{}/'.format(pruning_session))
        args['vb_init']=(-1, vb)
        args['hb_init']=(-2, hb)
        args['n_hidden'] = (nh1, nh2)
        args['prune']=True
        args['freeze_weights']=keep2
        args['w_init']=(0.1, new_weights2, 0.1)

        print("Shape of new weights of RBM2", new_weights2.shape)

        # initialise new RBM2
        rbm2_pruned = init_rbm2(Struct(**args))

        print("\nInitialize hidden unit particles for DBM...")

        Q_train = rbm1_pruned.transform(bin_X_train)
        Q_train_bin = make_probs_binary(Q_train)

        G_train = rbm2_pruned.transform(Q_train_bin)
        G_train_bin = make_probs_binary(G_train)

        ############## INITIALIZE PRUNED DBM ###################

        # initialize new DBM
        args['dbm_dirpath']=os.path.join(model_path,'MNIST_PrunedDBM_both_Sess{}/'.format(pruning_session))
        dbm_pruned = init_dbm(bin_X_train, None, (rbm1_pruned, rbm2_pruned), Q_train_bin, G_train_bin, Struct(**args))

        #run on gpu
        config = tf.ConfigProto(
            device_count = {'GPU': 1})
        dbm_pruned._tf_session_config = config

        # do as many samples as training instances
        samples = dbm_pruned.sample_gibbs(n_gibbs_steps=SAMPLE_EVERY, save_model=False, n_runs=n_train)

        s_v = samples[:,:nv]
        s_h1 = samples[:,nv:nv+nh1]
        s_h2 = samples[:,nv+nh1:]

        mean_activity_v = np.mean(s_v, axis=0)
        mean_activity_h1 = np.mean(s_h1, axis=0)
        mean_activity_h2 = np.mean(s_h2, axis=0)

        np.save(os.path.join(res_path,'mean_activity_v_both_Sess{}_before_retrain'.format(pruning_session)), mean_activity_v)
        np.save(os.path.join(res_path,'mean_activity_h1_both_Sess{}_before_retrain'.format(pruning_session)), mean_activity_h1)
        np.save(os.path.join(res_path,'mean_activity_h2_both_Sess{}_before_retrain'.format(pruning_session)), mean_activity_h2)

        ############ EVALUATION 1 ##############

        checkpoint=0
        res_n_hid_L2[it, checkpoint] = nh2 # save number of left hiddens in layer 2
        res_n_hid_L1[it, checkpoint] = nh1

        # get masks
        masks = dbm_pruned.get_tf_params(scope='masks')
        rf_mask1 = masks['rf_mask']
        prune_mask1 = masks['prune_mask']
        rf_mask2 = masks['rf_mask_1']
        prune_mask2 = masks['prune_mask_1']

        print("\nPruning session", pruning_session, "checkpoint", checkpoint+1,"\n")
        print("After pruning both layers, before joint retraining")

        mask2 = rf_mask2 * prune_mask2
        active_weights2 = len(mask2.flatten()) - len(mask2[mask2==0].flatten())
        mask1 = rf_mask1 * prune_mask1
        active_weights1 = len(mask1.flatten()) - len(mask1[mask1==0].flatten())

        print("")
        print(active_weights1, "active weights in layer 1")
        print(active_weights2, "active weights in layer 2\n")

        res_act_weights_L2[it, checkpoint] = active_weights2
        res_act_weights_L1[it, checkpoint] = active_weights1

        # get parameters
        weights = dbm_pruned.get_tf_params(scope='weights')
        W1 = weights['W']
        hb1 = weights['hb']
        W2 = weights['W_1']
        hb2 = weights['hb_1']
        vb = weights['vb']

        # compute FI for first layer
        samples = np.hstack((s_v, s_h1))

        temp_mask1 = rf_mask1 * prune_mask1

        print("Computing FI for weights of layer 1")
        var_est1, heu_est1 = FI_weights_var_heur_estimates(samples, nv, nh1, W1) # call without mask

        if USE_VAR:
            fi_weights_after_joint_RBM1 = var_est1.reshape((nh1,nv)).T * temp_mask1   # VARIANCE ESTIMATE!
        else:
            fi_weights_after_joint_RBM1 = heu_est1.reshape((nh1,nv)).T * temp_mask1   # HEURISTIC ESTIMATE!

        np.save(os.path.join(res_path, 'FI_weights_RBM1_before_retrain_sess{}'.format(pruning_session)), fi_weights_after_joint_RBM1)

        samples = np.hstack((s_h1, s_h2))

        # compute FI for second layer
        temp_mask2 = rf_mask2 * prune_mask2

        print("Computing FI for weights of layer 2")
        var_est2, heu_est2 = FI_weights_var_heur_estimates(samples, nh1, nh2, W2) # call without mask

        if USE_VAR:
            fi_weights_after_joint_RBM2 = var_est2.reshape((nh2,nh1)).T * temp_mask2   # VARIANCE ESTIMATE
        else:
            fi_weights_after_joint_RBM2 = heu_est2.reshape((nh2,nh1)).T * temp_mask2   # HEURISTIC ESTIMATE

        np.save(os.path.join(res_path, 'FI_weights_RBM2_before_retrain_sess{}'.format(pruning_session)), fi_weights_after_joint_RBM2)

        print("\nEvaluate samples similarity to digits...")
        pred_probs_samples = logreg_digits.predict_proba(s_v)
        prob_winner_pred_samples = pred_probs_samples.max(axis=1)
        mean_qual = np.mean(prob_winner_pred_samples)
        print("Mean quality of samples", mean_qual, "Std: ", np.std(prob_winner_pred_samples))

        ind_winner_pred_samples = np.argmax(pred_probs_samples, axis=1)
        sample_class, sample_counts = np.unique(ind_winner_pred_samples, return_counts=True)
        dic = dict(zip(sample_class, sample_counts))
        print("sample counts per class",dic)

        probs=pred_probs_samples.max(axis=1)
        mean_qual_d = np.zeros(len(sample_class))

        for i in range(0,len(sample_class)):
            ind=np.where(ind_winner_pred_samples==sample_class[i])
            mean_qual_d[i] = np.mean(probs[ind])

        qual_d = [sample_class, mean_qual_d, sample_counts]

        print("Weighted (per class frequency) mean quality of samples", np.mean(mean_qual_d))

        np.save(os.path.join(res_path, 'ProbsWinDig_sess{}_checkpoint{}.npy'.format(pruning_session, checkpoint+1)), qual_d)

        print("\nEvaluate hidden unit representations...")
        final_train = dbm_pruned.transform(bin_X_train)
        final_test = dbm_pruned.transform(bin_X_test)

        print("\nTrain LogReg classifier on final hidden layer...")
        logreg_hid = LogisticRegression(multi_class='multinomial', solver='sag', max_iter=800, n_jobs=2, random_state=4444)
        logreg_hid.fit(final_train, y_train)
        logreg_acc = logreg_hid.score(final_test, y_test)
        print("classification accuracy of LogReg classifier", logreg_acc)
        res_acc_logreg[it, checkpoint] = logreg_acc

        save_results()

        # RETRAIN
        print("\nRetraining of DBM after pruning both layers...")
        dbm_pruned.fit(bin_X_train)

        # get parameters
        weights = dbm_pruned.get_tf_params(scope='weights')
        W1 = weights['W']
        hb1 = weights['hb']
        W2 = weights['W_1']
        hb2 = weights['hb_1']
        vb = weights['vb']

        checkpoint =1

        ############ EVALUATION 2 ##############
        # after retraining of DBM
        print("\nPruning session", pruning_session, "checkpoint", checkpoint+1,"\n")
        print("After pruning both layers, after joint retraining")

        active_weights2 = len(W2.flatten())- W2[(prune_mask2==0) | (rf_mask2==0)].shape[0]
        active_weights1 = len(W1.flatten())- W1[(prune_mask1==0) | (rf_mask1==0)].shape[0]

        print("")
        print(active_weights1, "active weights in layer 1")
        print(active_weights2, "active weights in layer 2\n")

        res_act_weights_L2[it, checkpoint] = active_weights2
        res_act_weights_L1[it, checkpoint] = active_weights1
        res_n_hid_L2[it, checkpoint] = nh2 # save number of left hiddens in layer 2
        res_n_hid_L1[it, checkpoint] = nh1

        print("\nSampling...")
        #run on gpu
        config = tf.ConfigProto(
            device_count = {'GPU': 1})
        dbm_pruned._tf_session_config = config

        # do as many samples as training instances
        samples = dbm_pruned.sample_gibbs(n_gibbs_steps=SAMPLE_EVERY, save_model=False, n_runs=n_train)

        s_v = samples[:,:nv]
        s_h1 = samples[:,nv:nv+nh1]
        s_h2 = samples[:,nv+nh1:]

        mean_activity_v = np.mean(s_v, axis=0)
        mean_activity_h1 = np.mean(s_h1, axis=0)
        mean_activity_h2 = np.mean(s_h2, axis=0)

        np.save(os.path.join(res_path,'mean_activity_v_both_Sess{}_retrained'.format(pruning_session)), mean_activity_v)
        np.save(os.path.join(res_path,'mean_activity_h1_both_Sess{}_retrained'.format(pruning_session)), mean_activity_h1)
        np.save(os.path.join(res_path,'mean_activity_h2_both_Sess{}_retrained'.format(pruning_session)), mean_activity_h2)

        samples = np.hstack((s_v, s_h1))

        # compute FI for first layer
        temp_mask1 = rf_mask1 * prune_mask1

        print("Computing FI for weights of layer 1")
        var_est1, heu_est1 = FI_weights_var_heur_estimates(samples, nv, nh1, W1) # call without mask

        if USE_VAR:
            fi_weights_after_joint_RBM1 = var_est1.reshape((nh1,nv)).T * temp_mask1   # VARIANCE ESTIMATE!
        else:
            fi_weights_after_joint_RBM1 = heu_est1.reshape((nh1,nv)).T * temp_mask1   # HEURISTIC ESTIMATE!

        np.save(os.path.join(res_path, 'FI_weights_RBM1_after_retrain_sess{}'.format(pruning_session)), fi_weights_after_joint_RBM1)


        samples = np.hstack((s_h1, s_h2))

        # compute FI for second layer
        temp_mask2 = rf_mask2 * prune_mask2

        print("Computing FI for weights of layer 2")

        var_est2, heu_est2 = FI_weights_var_heur_estimates(samples, nh1, nh2, W2) 

        if USE_VAR:
            fi_weights_after_joint_RBM2 = var_est2.reshape((nh2,nh1)).T * temp_mask2   # VARIANCE ESTIMATE
        else:
            fi_weights_after_joint_RBM2 = heu_est2.reshape((nh2,nh1)).T * temp_mask2   # HEURISTIC ESTIMATE

        np.save(os.path.join(res_path, 'FI_weights_RBM2_after_retrain_sess{}'.format(pruning_session)), fi_weights_after_joint_RBM2)

        print("\nEvaluate samples similarity to digits...")
        pred_probs_samples = logreg_digits.predict_proba(s_v)
        prob_winner_pred_samples = pred_probs_samples.max(axis=1)
        mean_qual = np.mean(prob_winner_pred_samples)
        print("Mean quality of samples", mean_qual, "Std: ", np.std(prob_winner_pred_samples))

        ind_winner_pred_samples = np.argmax(pred_probs_samples, axis=1)
        sample_class, sample_counts = np.unique(ind_winner_pred_samples, return_counts=True)
        dic = dict(zip(sample_class, sample_counts))
        print("sample counts per class",dic)

        probs=pred_probs_samples.max(axis=1)
        mean_qual_d = np.zeros(len(sample_class))

        for i in range(0,len(sample_class)):
            ind=np.where(ind_winner_pred_samples==sample_class[i])
            mean_qual_d[i] = np.mean(probs[ind])

        qual_d = [sample_class, mean_qual_d, sample_counts]

        print("Weighted (per class frequency) mean quality of samples", np.mean(mean_qual_d))

        np.save(os.path.join(res_path, 'ProbsWinDig_sess{}_checkpoint{}.npy'.format(pruning_session, checkpoint+1)), qual_d)

        print("\nEvaluate hidden unit representations...")
        final_train = dbm_pruned.transform(bin_X_train)
        final_test = dbm_pruned.transform(bin_X_test)

        print("\nTrain LogReg classifier on final hidden layer...")
        logreg_hid = LogisticRegression(multi_class='multinomial', solver='sag', max_iter=800, n_jobs=2, random_state=4444)
        logreg_hid.fit(final_train, y_train)
        logreg_acc = logreg_hid.score(final_test, y_test)
        print("classification accuracy of LogReg classifier", logreg_acc)
        res_acc_logreg[it, checkpoint] = logreg_acc

        save_results()

        # set these for next loop
        fi_weights2 = fi_weights_after_joint_RBM2
        fi_weights1 = fi_weights_after_joint_RBM1


if __name__ == '__main__':

    def check_positive(value):
        ivalue = int(value)
        if ivalue <= 0:
            raise argparse.ArgumentTypeError('Not a positive integer.')
        return ivalue
    
    parser = argparse.ArgumentParser(description = 'DBM Pruning')
    parser.add_argument('percentile', default=10, nargs='?', help='Percentage of weights removed in each iteration', type=int, choices=range(1, 100))
    parser.add_argument('n_pruning_session', default=10, nargs='?', help='Number of pruning sessions', type=check_positive)

    args = parser.parse_args()

    main(args.percentile, args.n_pruning_session)