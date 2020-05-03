from sisyphus import *
import copy

from config.asr import get_asr_dataset_stats, train_asr_config, decode_and_evaluate_asr_config
from config.data import prepare_data_librispeech, build_subwords
from config.ttf import prepare_ttf_data, get_ttf_dataset_stats, train_ttf_config, generate_speaker_embeddings,\
  decode_with_speaker_embeddings
from config.f2l import train_f2l_config, griffin_lim_ogg, convert_with_f2l

from recipe.corpus import SegmentCorpus, MergeCorpora, BlissAddTextFromBliss, BlissToZipDataset
from recipe.tts.corpus import VerifyCorpus
from recipe.util import AutoCleanup

Path = setup_path(__package__)


def main():
  # prepare the datasets in bliss and zip format

  with tk.block("data_preparation"):
    bliss_dict, zip_dict, transcription_text_dict = prepare_data_librispeech()

  # compute the subword codes and the ASR vocabulary
  bpe_codes, bpe_vocab, num_classes = build_subwords([bliss_dict['train-clean-100']], num_segments=10000,
                                                     name="librispeech-100")

  # extract the mean and the deviation based on the statistics from the baseline training data
  mean, stddev = get_asr_dataset_stats(zip_dict['train-clean-100'])

  # set the data-derived parameters for all ASR systems
  asr_global_parameter_dict = {
    'ext_norm_mean': mean,
    'ext_norm_std_dev': stddev,
    'ext_bpe_file': bpe_codes,
    'ext_vocab_file': bpe_vocab,
    'ext_num_classes': num_classes
  }

  # define the parameters for the initial convergence training
  initial_checkpoint_training_params = {
    'ext_partition_epoch': 20,
    'ext_training_zips': [zip_dict['train-clean-100']],
    'ext_dev_zips': [zip_dict['dev-clean'],
                     zip_dict['dev-other']],
    'ext_num_epochs': 80
  }

  # this dict contains all models that will be evaluated
  models = {}

  with tk.block("baseline_training"):

    # training of the initial model
    initial_checkpoint_training_params.update(asr_global_parameter_dict)

    asr_training_config = Path("returnn_configs/asr/train-clean-100.exp3.ctc.ogg.lrwarmupextra10.config")
    initial_training_job = train_asr_config(asr_training_config, "librispeech-100-initial-training",
                                            initial_checkpoint_training_params)

    # training and decoding of the baseline model
    baseline_training_params = copy.deepcopy(initial_checkpoint_training_params)
    baseline_training_params['ext_num_epochs'] = 250
    baseline_training_params['ext_partition_epoch'] = 5

    #baseline_training_params['import_model_train_epoch1'] = initial_training_job.models[80].model
    baseline_training_params['load'] = initial_training_job.models[80].model
    baseline_training_params.update(asr_global_parameter_dict)
    continued_training_job = train_asr_config(asr_training_config, "librispeech-100-baseline-training",
                                              baseline_training_params)

    from recipe.returnn.search import GetBestEpoch
    best_epoch = GetBestEpoch(continued_training_job.model_dir, continued_training_job.learning_rates,
                              key="dev_score_output/output_prob").out_var

    models['baseline'] = (continued_training_job, best_epoch)

  with tk.block("specaug_training"):
    asr_specaug_config = Path("returnn_configs/asr/train-clean-100.exp3.ctc.ogg.lrwarmupextra10.specaug.config")

    # training and decoding of the specaug model
    continued_training_job = train_asr_config(asr_specaug_config, "librispeech-100-specaug-training",
                                              baseline_training_params)

    from recipe.returnn.search import GetBestEpoch
    best_epoch = GetBestEpoch(continued_training_job.model_dir, continued_training_job.learning_rates,
                              key="dev_score_output/output_prob").out_var

    models['specaug'] = (continued_training_job, best_epoch)




  ###########################
  # TTS
  ###########################

  tts_bliss_dict = {k:v for k,v in bliss_dict.items() if k in ['dev-clean', 'train-clean-100', 'train-clean-360']}

  # this will run the preprocessing and add a "tts-" prefix to the corpus names
  tts_bliss_corpora, tts_zip_corpora, char_vocab = prepare_ttf_data(tts_bliss_dict)

  mean, stddev = get_ttf_dataset_stats(tts_zip_corpora['tts-train-clean-100'])

  # set the data-derived parameters for the TTS system
  # also add the global epoch and partitioning settings
  tts_global_parameter_dict = {
    'ext_norm_mean_value': mean,
    'ext_norm_std_dev_value': stddev,
    'ext_char_vocab': char_vocab,
    'ext_training_zips': [tts_zip_corpora['tts-train-clean-100']],
    'ext_dev_zips': [tts_zip_corpora['tts-dev-clean']],
    'ext_num_epochs': 200,
    'ext_partition_epoch': 3,
  }

  # define the tts training config and run the training
  tts_training_config = Path("returnn_configs/tts/tts-clean-100.dec640.enc256.enclstm512.config",
                             hash_overwrite="TTS_DEC640_ENC256_ENCLSTM512_v1")
  tts_training_job = train_ttf_config(tts_training_config, name="tts-baseline-training",
                                      parameter_dict=tts_global_parameter_dict)


  # copy the dataset-derived parameters for the f2l training
  f2l_global_parameter_dict = copy.deepcopy(tts_global_parameter_dict)
  f2l_global_parameter_dict['ext_num_epochs'] = 100
  f2l_global_parameter_dict['ext_partition_epoch'] = 1
  f2l_global_parameter_dict.pop('ext_char_vocab')


  # define the config for the mel-to-linear feature conversion model and run the training
  f2l_training_config = Path("returnn_configs/f2l/f2l.2layer.blstm.residual.config",
                             hash_overwrite="F2L_2LAYER_ENC256_ENCLSTM512_v1")
  f2l_training_job = train_ttf_config(f2l_training_config, name="f2l-baseline-training",
                                      parameter_dict=f2l_global_parameter_dict)


  # generate the speaker embeddings using the GST network of the TTS model for all utterances in the training corpus
  embeddings = generate_speaker_embeddings(config_file=tts_training_config,
                              model_dir=tts_training_job.model_dir,
                              epoch=200,
                              zip_corpus=tts_zip_corpora['tts-train-clean-100'],
                              name="tts-baseline",
                              default_parameter_dict=tts_global_parameter_dict)

  from recipe.tts.corpus import DistributeSpeakerEmbeddings

  # randomly distribute the speaker embeddings for the sentences in the "text-only" data
  dist_speaker_embeds_job = DistributeSpeakerEmbeddings(tts_bliss_dict['train-clean-360'], embeddings,
                                                        use_full_seq_name=False, options=None)
  tk.register_output("embed_dist.hdf", dist_speaker_embeds_job.out)


  # the generation will be split in to N successive runs with cleaning in between to reduce the file space consumtion
  TTS_GENERATION_SPLITS = 10

  segment_job = SegmentCorpus(tts_bliss_corpora['tts-train-clean-360'], TTS_GENERATION_SPLITS)
  segments =segment_job.segment_files

  verification_result = None
  corpora = []

  for i in range(TTS_GENERATION_SPLITS):

    # run the tts decoding on the segmented part of the "text-only" data
    unstacked_features, decode_job, convert_job = decode_with_speaker_embeddings(config_file=tts_training_config,
                                                      model_dir=tts_training_job.model_dir,
                                                      epoch=200,
                                                      zip_corpus=tts_zip_corpora['tts-train-clean-360'],
                                                      speaker_hdf=dist_speaker_embeds_job.out,
                                                      segment_file=segments[i],
                                                      name="tts-baseline_decode_%i" % i,
                                                      default_parameter_dict=tts_global_parameter_dict)

    # we add the verification result of the previous decoding to ensure sequential execution of the jobs
    if verification_result:
        decode_job.add_input(verification_result)

    # run the f2l system to convert the log-mel features to linear spectograms
    linear_features, f2l_job = convert_with_f2l(f2l_training_config,
                                       name="tts-baseline_forward_%i" % i,
                                       features=unstacked_features,
                                       model_dir=f2l_training_job.model_dir,
                                       epoch=100)

    # run the Griffin & Lim synthesis and store the ogg files
    generated_audio_bliss, gl_job = griffin_lim_ogg(linear_features, name="tts-baseline_gl_%i" % i)

    # verify that all data is complete and no file was corrupted
    verification_result = VerifyCorpus(generated_audio_bliss).out

    # automatic cleanup of the jobs that are not needed
    # remove this for debugging purposes
    # WARNING: high HDD consumption
    cleanup_success = AutoCleanup([decode_job, convert_job, f2l_job], verification_result).out
    tk.register_output("cleanup_result/cleanup_%i" % i, cleanup_success)

    corpora.append(generated_audio_bliss)

  # merge the splitted audio corpora back to one corpus and add the original text from Librispeech-360h
  merge_job = MergeCorpora(corpora, "synthetic-ls-360", subcorpora=False)

  # confirm that the last corpus was correct before running the merge
  merge_job.add_input(verification_result)

  # add the original text in LibriSpeech format to the synthesized audio
  synthetic_audio_corpus = merge_job.merged_corpus
  synthetic_corpus = BlissAddTextFromBliss(synthetic_audio_corpus, bliss_dict['train-clean-360']).out

  # pack the corpus into the Zip format for RETURNN ASR training
  synthetic_zip_corpus = BlissToZipDataset("synthetic-ls-360", synthetic_corpus, use_full_seq_name=False).out
  tk.register_output("synthetic_data/synthetic_librispeech_360h.zip", synthetic_zip_corpus)


  # training with synthetic data
  with tk.block("baseline_with_synthetic"):
    synthetic_training_params = copy.deepcopy(initial_checkpoint_training_params)
    synthetic_training_params['ext_num_epochs'] = 250

    synthetic_training_params['load'] = initial_training_job.models[80].model
    synthetic_training_params.update(asr_global_parameter_dict)

    synthetic_training_params['ext_training_zips'] = [zip_dict['train-clean-100'], synthetic_zip_corpus]

    synthetic_training_job = train_asr_config(asr_training_config, "librispeech-100-synthetic-training",
                                              synthetic_training_params)

    from recipe.returnn.search import GetBestEpoch
    best_epoch = GetBestEpoch(continued_training_job.model_dir, continued_training_job.learning_rates,
                              key="dev_score_output/output_prob").out_var

    models['baseline+synthetic'] = (synthetic_training_job, best_epoch)

  # training with synthetic data and specaugment
  with tk.block("specaug_with_synthetic"):
    synthetic_training_params = copy.deepcopy(initial_checkpoint_training_params)
    synthetic_training_params['ext_num_epochs'] = 250

    synthetic_training_params['load'] = initial_training_job.models[80].model
    synthetic_training_params.update(asr_global_parameter_dict)

    synthetic_training_params['ext_training_zips'] = [zip_dict['train-clean-100'], synthetic_zip_corpus]

    synthetic_training_job = train_asr_config(asr_specaug_config, "librispeech-100-specaug-synthetic-training",
                                              synthetic_training_params)

    from recipe.returnn.search import GetBestEpoch
    best_epoch = GetBestEpoch(continued_training_job.model_dir, continued_training_job.learning_rates,
                              key="dev_score_output/output_prob").out_var

    models['specaug+synthetic'] = (synthetic_training_job, best_epoch)


  ############################
  # Evaluation
  ############################

  for experiment_name, (training_job, best_epoch) in models.items():
    with tk.block("%s_decoding" % experiment_name):
      for key in transcription_text_dict:
        wer = decode_and_evaluate_asr_config(key,
                                             asr_training_config,
                                             training_job.model_dir,
                                             epoch=best_epoch,
                                             zip_corpus=zip_dict[key],
                                             text=transcription_text_dict[key],
                                             parameter_dict=asr_global_parameter_dict,
                                             training_name=experiment_name)
        tk.register_output("results/%s_%s.wer" % (experiment_name, key), wer)
