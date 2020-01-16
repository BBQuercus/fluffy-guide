import itertools
import datetime
import luigi
import numpy as np
import tensorflow as tf

import data.dirtools
import data.provider
import models.unet
import models.resunet
import models.metrics
import models.callbacks
import config


class ConvertImagesNpy(luigi.Task):
    '''
    Converts images and masks for training/validation and testing
    into numpy files for easier access later on. Does not do any
    preprocessing or file manipulation.

    Args:
        dir_in (dir): Path to data files in the following format.
            Brackets denote multiple possibilties.
            dir_in/(train_val, test)/(images, masks)/(...jpg/tif/...)
        dir_out (dir): Path to where numpy files should be saved.
    '''

    dir_in = luigi.parameter.Parameter(default='../data/raw')
    dir_out = luigi.parameter.Parameter(default='../data/processed')

    def output(self):
        return {
            'train_val_images': luigi.LocalTarget(f'{self.dir_out}/train_val_images.npy'),
            'train_val_masks': luigi.LocalTarget(f'{self.dir_out}/train_val_masks.npy'),
            'train_images': luigi.LocalTarget(f'{self.dir_out}/train_images.npy'),
            'train_masks': luigi.LocalTarget(f'{self.dir_out}/train_masks.npy'),
            'val_images': luigi.LocalTarget(f'{self.dir_out}/val_images.npy'),
            'val_masks': luigi.LocalTarget(f'{self.dir_out}/val_masks.npy'),
            'test_images': luigi.LocalTarget(f'{self.dir_out}/test_images.npy'),
            'test_masks': luigi.LocalTarget(f'{self.dir_out}/test_masks.npy')
        }

    def run(self):

        # Import lists
        train_val_images, train_val_masks = data.dirtools.get_file_lists_seg(f'{self.dir_in}/train_val')
        test_images, test_masks = data.dirtools.get_file_lists_seg(f'{self.dir_in}/test')

        # Train / validation split
        train_images, train_masks, val_images, val_masks = data.dirtools.train_valid_split(train_val_images, train_val_masks)

        # Save to numpy
        data.dirtools.file_list_to_npy(train_val_images, self.output()['train_val_images'])
        data.dirtools.file_list_to_npy(train_val_masks, self.output()['train_val_masks'])
        data.dirtools.file_list_to_npy(train_images, self.output()['train_images'])
        data.dirtools.file_list_to_npy(train_masks, self.output()['train_masks'])
        data.dirtools.file_list_to_npy(val_images, self.output()['val_images'])
        data.dirtools.file_list_to_npy(val_masks, self.output()['val_masks'])
        data.dirtools.file_list_to_npy(test_images, self.output()['test_images'])
        data.dirtools.file_list_to_npy(test_masks, self.output()['test_masks'])


class TrainOneModel(luigi.Task):
    '''
    Trains one model from start to end including data preprocessing,
    model generation, validation, training. Does not include
    hyperparameter tuning, crossvalidation, score reporting.

    Args:
        config (dict): Dictionary containing all configurations.
        uuid (str): "Unique User ID" or unique identifier for naming.
        dir_out (dir): Path where model and logs should be saved.
    '''

    config = luigi.parameter.DictParameter(default=config.defaults)
    uuid = luigi.parameter.Parameter(default=datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    dir_out = luigi.parameter.Parameter(default='../models')

    def requires(self):
        return ConvertImagesNpy()

    def output(self):
        return {
            'config': luigi.LocalTarget(f'{self.dir_out}/config/{self.uuid}.csv'),
            'graph': luigi.LocalTarget(f'{self.dir_out}/graph/{self.uuid}.png'),
            'model_best': luigi.LocalTarget(f'{self.dir_out}/models/{self.uuid}.h5'),
            'model_final': luigi.LocalTarget(f'{self.dir_out}/models/{self.uuid}_final.h5'),
            'history': luigi.LocalTarget(f'{self.dir_out}/history/{self.uuid}.csv'),
            'tensorboard': luigi.LocalTarget(f'{self.dir_out}/tensorboard/{self.uuid}/')
        }

    def run(self):

        # Import data
        train_images = np.load(self.input()['train_images'].path, allow_pickle=True)
        train_masks = np.load(self.input()['train_masks'].path, allow_pickle=True)
        val_images = np.load(self.input()['val_images'].path, allow_pickle=True)
        val_masks = np.load(self.input()['val_masks'].path, allow_pickle=True)

        # Prepare generators
        config_preprocess = {
            'img_size': self.config['img_size'],
            'bit_depth': self.config['bit_depth'],
            'batch_size': self.config['batch_size'],
        }
        train_generator = data.provider.generator_seg(train_images, train_masks, training=True, **config_preprocess)
        val_generator = data.provider.generator_seg(val_images, val_masks, training=False, **config_preprocess)

        # Build model
        config_model = {
            'img_size': self.config['img_size'],
            'depth': self.config['depth'],
            'width': self.config['width'],
            'classes': self.config['classes'],
        }
        model = models.unet.model_seg(**config_model)
        tf.keras.utils.plot_model(model, to_file=self.output()['graph'].path, show_shapes=True)
        model.summary()

        # Compile model
        # TODO assert metrics from models.metrics
        # TODO auto add categorical / binary metrics?
        config_compile = {
            'optimizer': tf.keras.optimizers.Adam(self.config['lr']),
            'loss': tf.keras.losses.CategoricalCrossentropy(),
            'metrics': [tf.keras.metrics.CategoricalAccuracy()],
        }
        model.compile(**config_compile)

        # Train model
        callbacks = models.callbacks.defaults(
            self.output()['model_best'].path,
            self.output()['history'].path,
            self.output()['tensorboard'].path
        )
        config_training = {
            'steps_per_epoch': len(train_images) // self.config['batch_size'],
            'epochs': self.config['epochs'],
            'callbacks': callbacks,
            'validation_data': val_generator,
            'validation_freq': self.config['validation_freq'],
            'validation_steps': len(val_images) // self.config['batch_size'],
        }
        model.fit_generator(train_generator, **config_training)
        model.save(self.output()['model_final'].path)

        # Save final configs
        config_final = {
            **config_preprocess,
            **config_model,
            **config_compile,
            **config_training
        }
        data.dirtools.dict_to_csv(config_final, self.output()['config'].path)


class HParameterOptimizer(luigi.WrapperTask):
    '''
    '''
    # lrs = np.linspace
    # lrs = np.logspace
    # lrs = sklearn.model_selection.RandomizedSearchCV
    config = config.defaults

    def requires(self):
        for lr in self.lrs:
            config_adjusted = {
                'lr': lr,
            }
            self.config.update(config_adjusted)

            uuid = 'hmm'  # TODO

            yield TrainOneModel(config=self.config, uuid=uuid)


def main():
    hparam_lr = [0.1, 0.01, 0.001, 0.0001]
    hparam_width = [4, 16, 64]
    hparam_depth = [1, 2, 4]

    for lr, width, depth in itertools.product(
            hparam_lr, hparam_width, hparam_depth):

        config_new = {
            'lr': lr,
            'width': width,
            'depth': depth
        }
        config_curr = config.defaults
        config_curr.update(config_new)

        uuid = f'lr-{lr}_w-{width}_d-{depth}'

        luigi.build([TrainOneModel(config_curr, uuid)], local_scheduler=True)


if __name__ == "__main__":
    main()


# class CrossValidation(luigi.Task):

#     def

# def cross_validate(session, split_size=5):
#     results = []
#     kf = sklearn.model_selection.KFold(n_splits=split_size)
#     for train_idx, val_idx in kf.split(train_x_all, train_y_all):
#         train_x = train_x_all[train_idx]
#         train_y = train_y_all[train_idx]
#         val_x = train_x_all[val_idx]
#         val_y = train_y_all[val_idx]
#         run_train(session, train_x, train_y)
#         results.append(session.run(accuracy, feed_dict={x: val_x, y: val_y}))
#     return results

# Parsing functionality from https://github.com/divamgupta/image-segmentation-keras/blob/master/keras_segmentation/cli_interface.py

# def visualize_dataset_action(command_parser):
#     parser = command_parser.add_parser('visualize_dataset')
#     parser.add_argument("--images_path", type=str)
#     parser.add_argument("--segs_path", type=str)
#     parser.add_argument("--n_classes", type=int)
#     parser.add_argument('--do_augment', action='store_true')
#     def action(args):
#         visualize_segmentation_dataset(args.images_path, args.segs_path,
#                                     args.n_classes, do_augment=args.do_augment)
#     parser.set_defaults(func=action)

# def main():
#     assert len(sys.argv) >= 2, \
#         "python -m keras_segmentation <command> <arguments>"
#     main_parser = argparse.ArgumentParser()
#     command_parser = main_parser.add_subparsers()
#     # Add individual commands
#     train_action(command_parser)
#     predict_action(command_parser)
#     verify_dataset_action(command_parser)
#     visualize_dataset_action(command_parser)
#     evaluate_model_action(command_parser)
#     args = main_parser.parse_args()
#     args.func(args)