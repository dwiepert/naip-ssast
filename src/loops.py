"""
Model loops for pretraining, finetuning, and extracting embeddings from SSAST models

Last modified: 05/2023
Author: Daniela Wiepert
Email: wiepert.daniela@mayo.edu
File: loops.py
"""
#IMPORTS
#built-in
import json
import os

#third party
import numpy as np
import pandas as pd
import torch
from tqdm import tqdm
from sklearn.metrics import roc_auc_score, roc_curve

#local
from utilities import *

def pretrain(model, dataloader_train, dataloader_val = None, 
             optim='adamw', learning_rate=0.001,
             scheduler='onecycle', max_lr=0.01,
             epochs=10, cluster=False, task='pretrain_joint', mask_patch=400,
             exp_dir='', cloud=False, cloud_dir='', bucket=None):
    """
    Training loop for pretraining the SSAST 
    :param args: dict with all the argument values need for running
    :param model: SSAST model
    :param dataloader_train: dataloader object with training data
    :param dataloader_val: dataloader object with validation data
    :param optim: type of optimizer to initialize
    :param learning_rate: optimizer learning rate
    :param scheduler: type of scheduler to initialize
    :param max_lr: max learning rate for onecycle scheduler
    :param epochs: number of epochs to run pretraining
    :param cluster: cluster masking
    :param task: pretraining task 
    :param mask_patch: how many patches to mask (used only for ssl pretraining)
    :param exp_dir: output directory on local machine
    :param cloud: boolean indicating whether uploading to cloud
    :param cloud_dir: output directory in google cloud storage bucket
    :param bucket: initialized GCS bucket object
    :return model: pre-trained SSAST model
    """
    print('Pretraining start')
    #send to gpu
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)

    #optimizer
    if optim == 'adam':
        optimizer = torch.optim.Adam([p for p in model.parameters() if p.requires_grad],lr=learning_rate)
    elif optim == 'adamw':
         optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=learning_rate)
    else:
        raise ValueError(f'Given optimizer ({optim}) not supported.')
    
    if scheduler == 'onecycle':
        scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=max_lr, steps_per_epoch=len(dataloader_train), epochs=epochs)
    else:
        scheduler = None

    #train
    for e in range(epochs):
        training_loss = list()
        training_acc = list()
        #t0 = time.time()
        model.train()
        for batch in tqdm(dataloader_train):
            x = batch['fbank']
            targets = batch['targets']
            x, targets = x.to(device), targets.to(device)
            optimizer.zero_grad()
        
            if task == 'pretrain_mpc':
                acc, loss = model(x, task, mask_patch=mask_patch, cluster=cluster)
                # this is for multi-gpu support, in our code, loss is calculated in the model
                # pytorch concatenates the output of each gpu, we thus get mean of the losses of each gpu
                acc, loss = acc.mean(), loss.mean()
            # if pretrain with generative objective
            elif task == 'pretrain_mpg':
                loss = model(x, task, mask_patch=mask_patch, cluster=cluster)
                loss = loss.mean()
                # dirty code to make the code report mse loss for generative objective
                acc = loss
            # if pretrain with joint discriminative and generative objective
            elif task == 'pretrain_joint':
                acc, loss1 = model(x, 'pretrain_mpc', mask_patch=mask_patch, cluster=cluster)
                acc, loss1 = acc.mean(), loss1.mean()
                loss2 = model(x, 'pretrain_mpg', mask_patch=mask_patch, cluster=cluster)
                loss2 = loss2.mean()
                loss = loss1 + 10 * loss2

            loss.backward()
            optimizer.step()
            if scheduler is not None:
                scheduler.step()

            training_loss.append(loss.detach().cpu().item())
            training_acc.append(acc.detach().cpu().item())

        if e % 10 == 0:
            #SET UP LOGS
            if scheduler is not None:
                lr = scheduler.get_last_lr()
            else:
                lr = learning_rate
            logs = {'epoch': e, 'optim':optim, 'lr': lr}

            logs['training_loss_list'] = training_loss
            training_loss = np.array(training_loss)
            logs['running_loss'] = np.sum(training_loss)
            logs['training_loss'] = np.mean(training_loss)

            print('RUNNING LOSS', e, np.sum(training_loss) )
            print(f'Training loss: {np.mean(training_loss)}')

            logs['training_acc_list'] = training_acc
            training_acc = np.array(training_acc)
            logs['training_acc'] = np.mean(training_acc)
        
            print(f'Training acc: {np.mean(training_acc)}')

            if dataloader_val is not None:
                print("Validation start")
                validation_loss, validation_acc = validation_mask(model, dataloader_val, task, cluster, mask_patch)

                logs['val_loss_list'] = validation_loss
                validation_loss = np.array(validation_loss)
                logs['val_running_loss'] = np.sum(validation_loss)
                logs['val_loss'] = np.mean(validation_loss)
                
                print('RUNNING VALIDATION LOSS',e, np.sum(validation_loss) )
                print(f'Validation loss: {np.mean(validation_loss)}')

                logs['val_acc_list'] = validation_acc
                validation_acc = np.array(validation_acc)
                logs['val_acc'] = np.mean(validation_acc)

                print(f'Validation acc: {np.mean(validation_acc)}')
            
            #SAVE LOGS
            json_string = json.dumps(logs)
            logs_path = os.path.join(exp_dir, 'logs_pt_epoch{}.json'.format(e))
            with open(logs_path, 'w') as outfile:
                json.dump(json_string, outfile)
            
            #SAVE CURRENT MODEL
            print(f'Saving epoch {e}')
            mdl_path = os.path.join(exp_dir, 'ast_mdl_pt_epoch{}.pt'.format(e))
            torch.save(model.state_dict(), mdl_path)

            optim_path = os.path.join(exp_dir, 'ast_optim_pt_epoch{}.pt'.format(e))
            torch.save(optimizer.state_dict(), optim_path)
            
            if cloud:
                upload(cloud_dir, logs_path, bucket)
                #upload_from_memory(model.state_dict(), args.cloud_dir, mdl_path, args.bucket)
                upload(cloud_dir, mdl_path, bucket)
                upload(cloud_dir, optim_path, bucket)
    print('Pretraining finished')
    return model


def validation_mask(model, dataloader_val, task, cluster, mask_patch):
    '''
    Validation loop for pretraining with SSAST
    :param model: AST model
    :param dataloader_val: dataloader object with validation data
    :param task: pretraining task 
    :param cluster: cluster masking
    :param mask_patch: how many patches to mask (used only for ssl pretraining)
    :return validation_loss: list with validation loss for each batch
    :return validation_acc: list with validation accuracy for each batch
    '''
    validation_loss = list()
    validation_acc = list()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    with torch.no_grad():
        model.eval()
        for batch in tqdm(dataloader_val):
            x = batch['fbank']
            targets = batch['targets']
            x, targets = x.to(device), targets.to(device)
            # always use mask_patch=400 for evaluation, even the training mask patch number differs.
            if task == 'pretrain_mpc':
                acc, nce = model(x, task, mask_patch=mask_patch, cluster=cluster)
                validation_loss.append(nce.detach().cpu().item())
                validation_acc.append(acc.detach().cpu().item())
            elif task == 'pretrain_mpg':
                mse = model(x, task, mask_patch=mask_patch, cluster=cluster)
                # this is dirty code to track mse loss, A_acc and A_nce now track mse, not the name suggests
                validation_loss.append(mse.detach().cpu().item())
                validation_acc.append(mse.detach().cpu().item())
            elif task == 'pretrain_joint':
                acc, _ = model(x, 'pretrain_mpc', mask_patch=mask_patch, cluster=cluster)
                mse = model(x, 'pretrain_mpg', mask_patch=mask_patch, cluster=cluster)

                validation_loss.append(mse.detach().cpu().item())
                validation_acc.append(acc.detach().cpu().item())

    return validation_loss, validation_acc

def finetune(model, dataloader_train, dataloader_val = None, 
             optim='adamw', learning_rate=0.001, loss_fn='BCE',
             sched='onecycle', max_lr=0.01,
             epochs=10, exp_dir='', cloud=False, cloud_dir='', bucket=None):
    """
    Training loop for finetuning SSAST 
    :param model: SSAST model
    :param dataloader_train: dataloader object with training data
    :param dataloader_val: dataloader object with validation data
    :param optim: type of optimizer to initialize
    :param learning_rate: optimizer learning rate
    :param loss_fn: type of loss function to initialize
    :param sched: type of scheduler to initialize
    :param max_lr: max learning rate for onecycle scheduler
    :param epochs: number of epochs to run pretraining
    :param exp_dir: output directory on local machine
    :param cloud: boolean indicating whether uploading to cloud
    :param cloud_dir: output directory in google cloud storage bucket
    :param bucket: initialized GCS bucket object
    :return model: finetuned SSAST model
    """
    print('Finetuning start')
    #send to gpu
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    #loss
    if loss_fn == 'MSE':
        criterion = torch.nn.MSELoss()
    elif loss_fn == 'BCE':
        criterion = torch.nn.BCEWithLogitsLoss()
    else:
        raise ValueError(f'Given loss function ({loss_fn}) not supported. Must be either MSE or BCE')
    #optimizer
    if optim == 'adam':
        optimizer = torch.optim.Adam([p for p in model.parameters() if p.requires_grad],lr=learning_rate)
    elif optim == 'adamw':
         optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=learning_rate)
    else:
        raise ValueError(f'Given optimizer ({optim}) not supported. Must be either adam or adamw')
    
    if sched == 'onecycle':
        scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=max_lr, steps_per_epoch=len(dataloader_train), epochs=epochs)
    else:
        scheduler = None
    
    #train
    for e in range(epochs):
        training_loss = list()
        #t0 = time.time()
        model.train()
        for batch in tqdm(dataloader_train):
            x = batch['fbank']
            targets = batch['targets']
            x, targets = x.to(device), targets.to(device)
            optimizer.zero_grad()
            o = model(x)
            loss = criterion(o, targets)
            loss.backward()
            optimizer.step()
            if scheduler is not None:
                scheduler.step()
            loss_item = loss.item()
            training_loss.append(loss_item)

        if e % 10 == 0:
            #SET UP LOGS
            if scheduler is not None:
                lr = scheduler.get_last_lr()
            else:
                lr = learning_rate
            logs = {'epoch': e, 'optim':optim, 'loss_fn': loss_fn, 'lr': lr, 'scheduler':sched}
    
            logs['training_loss_list'] = training_loss
            training_loss = np.array(training_loss)
            logs['running_loss'] = np.sum(training_loss)
            logs['training_loss'] = np.mean(training_loss)

            print('RUNNING LOSS', e, np.sum(training_loss) )
            print(f'Training loss: {np.mean(training_loss)}')

            if dataloader_val is not None:
                print("Validation start")
                validation_loss = validation(model, criterion, dataloader_val)

                logs['val_loss_list'] = validation_loss
                validation_loss = np.array(validation_loss)
                logs['val_running_loss'] = np.sum(validation_loss)
                logs['val_loss'] = np.mean(validation_loss)
                
                print('RUNNING VALIDATION LOSS',e, np.sum(validation_loss) )
                print(f'Validation loss: {np.mean(validation_loss)}')
            
            #SAVE LOGS
            json_string = json.dumps(logs)
            logs_path = os.path.join(exp_dir, 'logs_ft_epoch{}.json'.format(e))
            with open(logs_path, 'w') as outfile:
                json.dump(json_string, outfile)
            
            #SAVE CURRENT MODEL
            print(f'Saving epoch {e}')
            mdl_path = os.path.join(exp_dir, 'ast_ft_mdl_epoch{}.pt'.format(e))
            torch.save(model.state_dict(), mdl_path)
            
            optim_path = os.path.join(exp_dir, 'ast_ft_optim_epoch{}.pt'.format(e))
            torch.save(optimizer.state_dict(), optim_path)

            if cloud:
                upload(cloud_dir, logs_path, bucket)
                upload(cloud_dir, mdl_path, bucket)
                upload(cloud_dir, optim_path, bucket)

    print('Finetuning finished')
    return model


def validation(model, criterion, dataloader_val):
    '''
    Validation loop for finetuning 
    :param model: SSAST model
    :param criterion: loss function
    :param dataloader_val: dataloader object with validation data
    :return validation_loss: list with validation loss for each batch
    '''
    validation_loss = list()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    with torch.no_grad():
        model.eval()
        for batch in tqdm(dataloader_val):
            x = batch['fbank']
            targets = batch['targets']
            x, targets = x.to(device), targets.to(device)
            o = model(x)
            val_loss = criterion(o, targets)
            validation_loss.append(val_loss.item())

    return validation_loss

def evaluation(model, dataloader_eval, exp_dir, cloud=False, cloud_dir=None, bucket=None):
    """
    Start model evaluation
    :param model: SSAST model
    :param dataloader_eval: dataloader object with evaluation data
    :param exp_dir: specify LOCAL output directory as str
    :param cloud: boolean to specify whether to save everything to google cloud storage
    :param cloud_dir: if saving to the cloud, you can specify a specific place to save to in the CLOUD bucket
    :param bucket: google cloud storage bucket object
    :return preds: model predictions
    :return targets: model targets (actual values)
    """
    print('Evaluation start')
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    outputs = []
    t = []
    model = model.to(device)
    with torch.no_grad():
        model.eval()
        for batch in tqdm(dataloader_eval):
            x = batch['fbank']
            x = x.to(device)
            targets = batch['targets']
            targets = targets.to(device)
            o = model(x)
            outputs.append(o)
            t.append(targets)

    outputs = torch.cat(outputs).cpu().detach()
    t = torch.cat(t).cpu().detach()
    # SAVE PREDICTIONS AND TARGETS 
    pred_path = os.path.join(exp_dir, 'ast_eval_predictions.pt')
    target_path = os.path.join(exp_dir, 'ast_eval_targets.pt')
    torch.save(outputs, pred_path)
    torch.save(t, target_path)

    if cloud:
        upload(cloud_dir, pred_path, bucket)
        upload(cloud_dir, target_path, bucket)

    print('Evaluation finished')
    return outputs, t

def embedding_extraction(model, dataloader, embedding_type='ft', layer=-1, task='ft_cls'):
    """
    Run a specific subtype of evaluation for getting embeddings.
    :param model: SSAST model
    :param dataloader_eval: dataloader object with data to get embeddings for
    :param embedding_type: string specifying whether embeddings should be extracted from classification head (ft) or base pretrained model (pt)
    :param layer: int indicating which hidden state layer to use.
    :param task: finetuning task, only used for 'pt' or 'wt' embedding extraction.
    :return embeddings: an np array containing the embeddings
    """

    print('Calculating Embeddings')
    embeddings = np.array([])
    # send to gpu
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = model.to(device)

    with torch.no_grad():
        model.eval()
        for batch in tqdm(dataloader):
            x = batch['fbank']
            x = x.to(device)
            e = model.extract_embedding(x, embedding_type, layer, task)
            if embeddings.size == 0:
                embeddings = e
            else:
                embeddings = np.append(embeddings, e, axis=0)

    return embeddings

def calc_auc(preds, targets, target_labels,
         exp_dir, cloud, cloud_dir, bucket):
    """
    Get AUC scores, doesn't return, just saves the metrics to a csv
    :param args: dict with all the argument values
    :param preds: model predictions
    :param targets: model targets (actual values)
    """
    #get AUC score and all data for ROC curve
    preds = preds[targets.isnan().sum(1)==0]
    targets[targets.isnan().sum(1)==0]
    pred_mat=torch.sigmoid(preds).numpy()
    target_mat=targets.numpy()
    aucs=roc_auc_score(target_mat, pred_mat, average = None) #TODO: this doesn't work when there is an array with all labels as 0???
    print(aucs)
    data = pd.DataFrame({'Label':target_labels, 'AUC':aucs})
    data.to_csv(os.path.join(exp_dir, 'aucs.csv'), index=False)
    if cloud:
        upload(cloud_dir, os.path.join(exp_dir, 'aucs.csv'), bucket)

    return data