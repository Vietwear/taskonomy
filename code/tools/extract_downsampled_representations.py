'''
  Name: extract.py
  Desc: Extract representations.
  Usage:
    python encode_inputs.py /path/to/cfgdir/ --gpu gpu_id
'''
from __future__ import absolute_import, division, print_function

import argparse
import os
import numpy as np
import pickle
import pdb
import tensorflow as tf
import tensorflow.contrib.slim as slim
import time
import threading

import init_paths
from   data.load_ops import resize_rescale_image, to_light
from   data.task_data_loading import load_and_specify_preprocessors_for_representation_extraction
import general_utils
from   general_utils import RuntimeDeterminedEnviromentVars
import models.architectures as architectures
from   models.sample_models import *
import utils

parser = argparse.ArgumentParser(description='Extract representations of encoder decoder model.')
parser.add_argument( '--cfg_dir', dest='cfg_dir', help='directory containing config.py file, should include a checkpoint directory' )
parser.set_defaults(cfg_dir="/home/ubuntu/task-taxonomy-331b/experiments/aws_second")
parser.add_argument('--gpu', dest='gpu_id',
                    help='GPU device id to use [0]',
                    type=int)
parser.add_argument('--nopause', dest='nopause', action='store_true')
parser.set_defaults(nopause=True)

parser.add_argument('--task', dest='task')
parser.add_argument('--data-split', dest='data_split')
parser.set_defaults(data_split="val")

parser.add_argument('--out-dir', dest='out_dir')
parser.set_defaults(out_dir="")


parser.add_argument('--representation-task', dest='representation_task')
parser.set_defaults(representation_task="")

def main( _ ):
    args = parser.parse_args()
    #task_list = ["autoencoder", "colorization","curvature", "denoise", "edge2d", "edge3d", "ego_motion", "fix_pose", "impainting", "jigsaw", "keypoint2d", "keypoint3d", "non_fixated_pose", "point_match", "reshade", "rgb2depth", "rgb2mist", "rgb2sfnorm", "room_layout", "segment25d", "segment2d", "vanishing_point"]
    #single channel for colorization !!!!!!!!!!!!!!!!!!!!!!!!! COME BACK TO THIS !!!!!!!!!!!!!!!!!!!!!!!!!!!
    task_list = [ args.task ]

    # Get available GPUs
    local_device_protos = utils.get_available_devices()
    print( 'Found devices:', [ x.name for x in local_device_protos ] )  
    # set GPU id
    if args.gpu_id:
        print( 'using gpu %d' % args.gpu_id )
        os.environ[ 'CUDA_VISIBLE_DEVICES' ] = str( args.gpu_id )
    else:
        print( 'no gpu specified' )
    
    for task in task_list:
        task_dir = os.path.join(args.cfg_dir, task)
        cfg = utils.load_config( task_dir, nopause=args.nopause )
        root_dir = cfg['root_dir']

        transfer = (cfg['model_type'] == architectures.TransferNet)
        if not transfer:
            # For siamese tasks, we only need one representation per image. Pairs are irrelevant.
            cfg['train_filenames'] = os.path.abspath( os.path.join( root_dir, 'assets/aws_data/train_split_image_info.pkl') )    
            cfg['val_filenames'] = os.path.abspath( os.path.join( root_dir, 'assets/aws_data/val_split_imag_info.pkl') )    
            cfg['test_filenames'] = os.path.abspath( os.path.join( root_dir, 'assets/aws_data/test_split_image_info.pkl') )  
            cfg['preprocess_fn'] = load_and_specify_preprocessors_for_representation_extraction
        else:
            cfg['train_filenames'] = os.path.abspath( os.path.join( root_dir, 'assets/aws_data/val_split_imag_info.pkl') )    
            cfg['val_filenames'] = os.path.abspath( os.path.join( root_dir, 'assets/aws_data/test_split_image_info.pkl') )    

        if args.data_split == 'train':
            split_file = cfg['train_filenames']
        elif args.data_split == 'val':
            split_file = cfg['val_filenames']
        elif args.data_split == 'test':
            split_file = cfg['test_filenames']
        else: 
            raise NotImplementedError("Unknown data split section: {}".format(args.data_split))
        cfg['train_filenames'] = split_file
        cfg['val_filenames'] = split_file
        cfg['test_filenames'] = split_file 
        if 'train_list_of_fileinfos' in cfg:
            split_file_ =  cfg['{}_representations_file'.format(args.data_split)]
            cfg['train_representations_file'] = split_file_
            cfg['val_representations_file'] = split_file_
            cfg['test_representations_file'] = split_file_

            split_file_ =  cfg['{}_list_of_fileinfos'.format(args.data_split)]
            cfg['train_list_of_fileinfos'] = split_file_
            cfg['val_list_of_fileinfos'] = split_file_
            cfg['test_list_of_fileinfos'] = split_file_
        if args.representation_task:
            split_file_ = args.representation_task
            if 'multiple_input_tasks' in cfg:
                split_file_ = [split_file_]
            cfg['train_representations_file'] = split_file_
            cfg['val_representations_file'] = split_file_
            cfg['test_representations_file'] = split_file_
  
        if cfg['model_path'] is None:
            cfg['model_path'] = os.path.join(cfg['dataset_dir'], "model_log", task, "model.permanent-ckpt") 
        if cfg['model_type'] == architectures.TransferNet:
            cfg['model_path'] = os.path.join(cfg['log_root'], task, "model.permanent-ckpt") 
        cfg['randomize'] = False
        cfg['num_epochs'] = 1
        cfg['num_read_threads'] = 1


        cfg['input_dim'] = (32, 32)  # (1024, 1024)
        cfg['input_num_channels'] = 1
        cfg['input_dtype'] = tf.float32
        cfg['input_domain_name'] = 'rgb'
        cfg['input_preprocessing_fn'] = to_light
        cfg['input_preprocessing_fn_kwargs'] = {
            'new_dims': cfg['input_dim'],
            'new_scale': [-1, 1],
            'interp_order': 1
        }

        representation_dir = args.cfg_dir
        run_extract_representations( args, cfg, representation_dir, task )


def run_extract_representations( args, cfg, save_dir, given_task ):
    transfer = (cfg['model_type'] == architectures.TransferNet)
    if transfer:
        get_data_prefetch_threads_init_fn = utils.get_data_prefetch_threads_init_fn_transfer
        setup_input_fn = utils.setup_input_transfer
    else:
        setup_input_fn = utils.setup_input
        get_data_prefetch_threads_init_fn = utils.get_data_prefetch_threads_init_fn
    
    
    
    # set up logging
    tf.logging.set_verbosity( tf.logging.INFO )

    with tf.Graph().as_default() as g:
        # create ops and placeholders
        tf.logging.set_verbosity( tf.logging.INFO )
        inputs = setup_input_fn( cfg, is_training=False, use_filename_queue=False )
        RuntimeDeterminedEnviromentVars.load_dynamic_variables( inputs, cfg )
        RuntimeDeterminedEnviromentVars.populate_registered_variables()
        
        # build model (and losses and train_op)

        # set up metrics to evaluate
        # names_to_values, names_to_updates = setup_metrics( inputs, model, cfg )

        # execute training 
        start_time = time.time()
        utils.print_start_info( cfg, inputs[ 'max_steps' ], is_training=False )

        # start session and restore model
        training_runners = { 'sess': tf.Session(), 'coord': tf.train.Coordinator() }
        try:
            if cfg['model_path'] is None:
                print('Please specify a checkpoint directory')
                return	
            
            utils.print_start_info( cfg, inputs[ 'max_steps' ], is_training=False )
            data_prefetch_init_fn = get_data_prefetch_threads_init_fn( inputs, cfg, 
                is_training=False, use_filename_queue=False )
            prefetch_threads = threading.Thread(
                target=data_prefetch_init_fn,
                args=( training_runners[ 'sess' ], training_runners[ 'coord' ] ))
            prefetch_threads.start()
            
            # run one example so that we can calculate some statistics about the representations
            filenames = []
            representations, data_idx = training_runners['sess'].run( [ 
                    inputs['input_batch'], inputs[ 'data_idxs' ] ] )        

            filenames.extend(data_idx)
            if type(representations) == list:
                representations = representations[0]
            representations = representations.reshape((-1, np.prod(cfg['input_dim'])))
            print( 'Got first batch representation with size: {0}'.format( representations.shape ) )

            # run the remaining examples
            for step in range( inputs[ 'max_steps' ] - 1 ):
            #for step in range( 10 ):
                if step % 100 == 0: 
                    print( 'Step {0} of {1}'.format( step, inputs[ 'max_steps' ] - 1 ))
                
                # This is just for GAN, for the LEO meeting
                encoder_output, data_idx = training_runners['sess'].run( [
                        inputs['input_batch'], inputs[ 'data_idxs' ] ] )        
                if type(encoder_output) == list:
                    encoder_output = encoder_output[0]
                representations = np.append(representations, encoder_output.reshape((-1, np.prod(cfg['input_dim']))), axis=0)
                filenames.extend(data_idx)

                if training_runners['coord'].should_stop():
                    break

            print('The size of representations is %s while we expect it to run for %d steps with batchsize %d' % (representations.shape, inputs['max_steps'], cfg['batch_size']))

            end_train_time = time.time() - start_time
            save_path = os.path.join( save_dir, '{task}_{split}_representations.pkl'.format(task='pixels', split=args.data_split) )

            with open( save_path, 'wb' ) as f:
                pickle.dump( {
                    'file_indexes': filenames, 
                    'representations': representations}, f )
            
            copy_to = None
            if args.out_dir:
                os.system("sudo cp {fp} {out}/".format(fp=save_path, out=args.out_dir))
                copy_to = args.out_dir
            else:
                if transfer:
                    os.system("sudo cp {fp} /home/ubuntu/s3/model_log/representations_transfer/".format(fp=save_path))
                    copy_to = '/home/ubuntu/s3/model_log/representations_transfer/'
                else:
                    os.system("sudo cp {fp} /home/ubuntu/s3/model_log/representations/".format(fp=save_path))
                    copy_to = "/home/ubuntu/s3/model_log/representations/"
            
            print( 'saved representations to {0}'.format( save_path ))
            print( 'copied representations to {0}'.format( copy_to ))
            print('time to extract %d epochs: %.3f hrs' % (cfg['num_epochs'], end_train_time/(60*60)))
        finally:
            utils.request_data_loading_end( training_runners )
            utils.end_data_loading_and_sess( training_runners )

def setup_metrics( inputs, model, cfg ):
    # predictions = model[ 'model' ].
    # Choose the metrics to compute:
    # names_to_values, names_to_updates = slim.metrics.aggregate_metric_map( {} )
    return  {}, {}

if __name__=='__main__':
    main( '' )
