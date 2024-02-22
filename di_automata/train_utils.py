""""Make Run class which contains all relevant logic for instantiating and training."""
from typing import Tuple, List, TypeVar
import typing
from pathlib import Path
from tqdm import tqdm
import wandb
import logging
import os
import shutil
import contextlib
import subprocess
from omegaconf import OmegaConf
from functools import partial
import s3fs
import pandas as pd
import math
import time
from datetime import datetime
from torch_ema import ExponentialMovingAverage

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from di_automata.devinterp.slt.sampler import estimate_learning_coeff_with_summary
from di_automata.devinterp.rlct_utils import (
    extract_and_save_rlct_data,
)
from di_automata.tasks.data_utils import take_n
from di_automata.config_setup import *
from di_automata.constructors import (
    construct_model, 
    optimizer_constructor,
    ema_constructor,
    initialise_model,
    create_dataloader_hf,
    construct_rlct_criterion,
    get_state_dict,
)
Sweep = TypeVar("Sweep")

# Path to root dir (with setup.py)
PROJECT_ROOT = Path(__file__).parent.parent

# AWS S3 bucket
s3 = s3fs.S3FileSystem()


class Run:
    def __init__(self, config: MainConfig):
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        self.config = config
        
        self.train_loader = create_dataloader_hf(self.config, deterministic=False)
        
        self.model, param_inf_properties = construct_model(config)
        self.model.to(self.device)
        # Initialise the model (possibly with muP, link: https://arxiv.org/pdf/2203.03466.pdf)
        initialise_model(config, self.model, param_inf_properties)
        # self.model = torch.compile(self.model) # Not supported for Windows. Bill Gates computer.
        
        self.optimizer, self.scheduler = optimizer_constructor(config, self.model, param_inf_properties)
        
        if self.config.use_ema:
            self.ema = ema_constructor(self.model, self.config.ema_decay)
            self.ema.to(self.device)
        else:
            self.ema = None

        self.rlct_data_list: list[dict[str, float]] = []
        self.rlct_folder = Path(__file__).parent / self.config.rlct_config.rlct_data_dir
        self.rlct_folder.mkdir(parents=True, exist_ok=True)
        self.rlct_criterion = construct_rlct_criterion(self.config)
        
        # Set time and use it as a distinguishing parameter for this run
        self.config["time"] = datetime.now().strftime("%m_%d_%H_%M")
        
        self._set_logger()
        
        # Now that WandB run is initialised, save config as artifact
        self._save_config()
    
    
    def train(self) -> None:
        criterion = nn.CrossEntropyLoss()
        self.train_acc_list, self.train_loss_list = [], []
        self.model.train()
        self.progress_bar = tqdm(total=self.config.num_training_iter)
        self.idx, self.epoch = 0, 0
        self.lr = self.config.optimizer_config.default_lr
        no_improve_count = 0
        best_loss = 1000
        
        for epoch in range(self.config.num_epochs):
            self.epoch = epoch
            train_loss = []
            print(f"Training epoch {epoch}")
            self.num_iter = self.config.eval_frequency if epoch + 1 <= self.config.num_training_iter / self.config.eval_frequency  else self.config.num_training_iter % self.config.eval_frequency
            
            for data in take_n(self.train_loader, self.num_iter): # Compatible with HF dataset format where data is a dictionary
                # all_idxs is a list of idxs (not useful)
                iter_model_saved = False
                self.idx += 1 # TODO: move this to end of loop for easier analysis indexing (always log step 0)
                inputs, labels = data["input_ids"].to(self.device), data["label_ids"].to(self.device)
                logits = self.model(inputs)

                self.optimizer.zero_grad()
                loss = criterion(logits, labels.long())
                detached_loss = loss.detach().cpu().item()
                loss.backward()
                self.optimizer.step()
                
                if self.config.optimizer_config.cosine_lr_schedule: # Code baked in for CustomLRScheduler instance for now 
                    self.scheduler.step()
                if self.config.use_ema:
                    self.ema.update()
                    
                train_loss.append(detached_loss)
                self.progress_bar.update()
                
                if self.idx % self.config.rlct_config.ed_config.eval_frequency == 0:
                    self._save_model() # Save model but do not log other metrics at this frequency
                    iter_model_saved = True
                
            train_loss, train_acc = self._evaluation_step()
            self.progress_bar.set_description(f"Epoch {epoch} accuracy {train_acc}")
            
            # Early logit saving in case run crashes
            if train_acc > 80 and epoch % 5 == 0:
                self._save_logits()
                
            # Early-stopping
            if math.log(train_loss) < math.log(best_loss):
                best_loss = train_loss
                no_improve_count = 0
            else:
                no_improve_count += 1
            if no_improve_count >= self.config.early_stop_patience or train_acc > self.config.early_stop_acc_threshold:
                print(f"Early stopping: log loss has not decreased in {self.config.early_stop_patience} steps.")
                return

            if self.config.llc_train: self._rlct_training()
            
            if not iter_model_saved: self._save_model()

            
    def _rlct_training(self) -> tuple[Union[float, pd.DataFrame], ...]:
        """Estimate RLCT for a given epoch during training.

        Needs to be called in same execution logic as eval_and_save() so that the logger uses the right step.
        Currently implemented for distillation training in main code.
        """
        # Use for initialising new model in sample function for LLC
        checkpoint = self.model.state_dict()
            
        rlct_func = partial(
            estimate_learning_coeff_with_summary,
            loader=self.train_loader,
            criterion=self.rlct_criterion,
            main_config=self.config,
            checkpoint=checkpoint,
            device=self.device,
        )
        
        results, callback_names = rlct_func(
            sampling_method=rlct_class_map[self.config.rlct_config.sampling_method], 
            optimizer_kwargs=self.config.rlct_config.sgld_kwargs
        )
        results_filtered = extract_and_save_rlct_data(results, callback_names, sampler_type=self.config.rlct_config.sampling_method.lower(), idx=self.idx)    
        self.rlct_data_list.append(results_filtered)
        
    
    def _save_rlct(self) -> None:
        rlct_df = pd.DataFrame(self.rlct_data_list)
        rlct_df.to_csv(self.rlct_folder / f"{self.config.run_name}.csv")
        rlct_artifact = wandb.Artifact(f"rlct_distill_{self.config.rlct_config.use_distill_loss}", type="rlct", description="RLCT mean and std for all samplers used.")
        rlct_artifact.add_file(self.rlct_folder / f"{self.config.run_name}.csv")
        wandb.log_artifact(rlct_artifact, aliases=[f"rlct_distill_{self.config.rlct_config.use_distill_loss}"])
    
    
    def _evaluation_step(self) -> tuple[float, float]:
        """TODO: consider test accuracy and loss."""
        train_acc, train_loss = evaluate(
            model=self.model, 
            data_loader=self.train_loader, 
            num_eval_batches=self.config.num_eval_batches,
            ema=self.ema if self.config.use_ema else None,
        )
        self.train_acc_list.append(train_acc)
        self.train_loss_list.append(train_loss)
        
        self.progress_bar.set_description(f'Project {self.config.wandb_config.wandb_project_name}, Epoch: {self.epoch}, Train Accuracy: {train_acc}, Train Loss: {train_loss}, LR {self.lr}')
        
        wandb.log({"Train Acc": train_acc, "Train Loss": train_loss, "LR": self.scheduler.get_lr()}, step=self.idx)
        
        return train_loss, train_acc


    def _save_config(self) -> None:
        """Only called once to prevent saving config multiple times."""
        model_artifact = wandb.Artifact(f"config", type="config", description="Config after run-time attributes filled in.")
        with open(".hydra/config.yaml", "w") as file:
            file.write(OmegaConf.to_yaml(self.config)) # Necessary: Hydra automatic config file does not include Pydantic run-time attributes and wrong thing will be logged and cause a nasty bug
        model_artifact.add_file(".hydra/config.yaml", name="config.yaml")
        wandb.log_artifact(model_artifact, aliases=[f"{self.config.run_name}"])
            
            
    def _save_model(self) -> None:
        """Checkpoint model to AWS, WandB or local. Latter two not recommended due to frequency of ED checkpoints."""
        context_manager = self.ema.average_parameters() if self.config.use_ema else no_op_context()
        # Saving moving average weights in state dict
        with context_manager:
            model_to_save = self.model.module if isinstance(self.model, torch.nn.DataParallel) else self.model
            state = get_state_dict(model_to_save, self.optimizer, self.scheduler, self.ema)
            torch.save(
                state, # Save optimizer, model and scheduler all in one go
                Path(".") / "states.torch", # Working directory configured by Hydra as output directory
            )
        
        match self.config.model_save_method:
            case "wandb":
                model_artifact = wandb.Artifact(f"states", type="states", description="The trained model state_dict")
                model_artifact.add_file(f"states.torch")
                wandb.log_artifact(model_artifact, aliases=[f"idx{self.idx}_{self.config.run_name}_{self.config.time}"])
                os.remove("states.torch") # Delete file to prevent clogging up
            case "aws":
                with s3.open(f'{self.config.aws_bucket}/{self.config.run_name}_{self.config.time}/{self.idx}.pth', mode='wb') as file:
                    torch.save(state, file)
                print("Saved model to AWS")
            case "local":
                file_path =  self.model_save_dir / f"{self.config.run_name}"
                data = {"Train Acc": self.train_acc_list, "Train Loss": self.train_loss_list}
                torch.save(dict(state, **data), f"{file_path}.torch")
        
        # Only delete cache every 20 steps to prevent race condition with WandB's upload thread
        if self.idx % 20 == 0 and self.config.model_save_method == "wandb":
            self._del_wandb_cache()
            
            
    def _clean_sweep(self, sweep: "Sweep") -> List["Sweep"]:
        """Get rid of wandb runs that crashed with _step = None."""
        def _clean_sweep():
            for r in sweep.runs:
                if r.summary.get("_step", None) is None:
                    r.delete()
                    yield r

        return list(r for r in _clean_sweep())
    
    
    def _set_logger(self) -> None:
        """Currently uses WandB as default."""
        logging.info(f"Hydra current working directory: {os.getcwd()}")
        logger_params = {
            "name": self.config.run_name,
            "project": self.config.wandb_config.wandb_project_name,
            "settings": wandb.Settings(start_method="thread"),
            "config": OmegaConf.to_container(self.config, resolve=True, throw_on_missing=True),
            "mode": "disabled" if not self.config.is_wandb_enabled else "online",
        }
        self.run = wandb.init(**logger_params, entity=self.config.wandb_config.entity_name)
        # Probably won't do sweeps over these - okay to put here relative to call to update_with_wandb_config() below
        wandb.config.dataset_type = self.config.task_config.dataset_type
        wandb.config.model_type = self.config.model_type
        
        # Location on remote GPU of WandB cache to delete periodically
        self.wandb_cache_dirs = [Path.home() / ".cache/wandb/artifacts/obj", Path.home() / "root/.cache/wandb/artifacts/obj"]
        """We also have "root/.local/share/wandb/artifacts", but this can't be deleted as often as doing so prevents proper upload of model checkpoints. Delete in finish_run() below."""
        
        
    def finish_run(self) -> None:
        """Clean up last RLCT calculation, save data, finish WandB run and delete large temporary folders.
        
        We define an extra cache dir to be removed at end of run here.
        """
        if self.config.llc_train:
            self._save_rlct()
            
        if self.config.is_wandb_enabled:
            wandb.finish()
            
            upload_cache_dir = Path.home() / "root/.local/share/wandb/artifacts/staging" 
            if upload_cache_dir.is_dir() and self.config.model_save_method == "wandb":
                shutil.rmtree(upload_cache_dir)
                
            time.sleep(60)
            shutil.rmtree("wandb")
        
    
    def _del_wandb_cache(self):
        command = ['wandb', 'artifact', 'cache', 'cleanup', "--remove-temp", "1GB"]
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except:
            print("Cache file deletion skipped.")
        
        for cache_dir in self.wandb_cache_dirs:
            if cache_dir.is_dir():
                try: 
                    shutil.rmtree(cache_dir)
                    print(f"Removed {cache_dir}")
                except OSError as e: 
                    print(f"Failed to remove dir {cache_dir}.", e)
                    
                    
    def _restore_model(self, resume_training: bool = False) -> None:
        """Restore from a checkpoint on WandB.
        
        TODO: Currently not in use and needs updating for EMA.
        """
        artifact = self.run.use_artifact(f"{self.config.wandb_config.entity_name}/{self.config.wandb_config.wandb_project_name}/states:epoch{self.epoch}_{self.config.run_name}")
        data_dir = artifact.download()
        config_path = Path(data_dir) / "config.yaml"
        model_state_path = Path(data_dir) / "states.torch"
        config = OmegaConf.load(config_path)
        self.config = typing.cast(MainConfig, config)
        states = torch.load(model_state_path)
        
        self.model_state_dict, optimizer_state_dict, scheduler_state_dict, ema_state_dict = states["model"], states["optimizer"], states["scheduler"], states["ema"]
        
        if resume_training:
            # Only need below code if a) passing ref model in and doing deepcopy;
            # b) using this function outside of RLCT estimation for e.g. resuming training
            model, _ = construct_model(self.config)
            model.load_state_dict(self.model_state_dict)
            model.to(self.device)
            model.train()
            # self.model = torch.compile(model) # Not available on Windows
            self.optimizer.load_state_dict(optimizer_state_dict)
            self.scheduler.load_state_dict(scheduler_state_dict)
            self.ema.load_state_dict(ema_state_dict)

           
@torch.no_grad()
def evaluate(
    model: nn.Module, 
    data_loader: DataLoader,
    num_eval_batches: int,
    ema: Optional[ExponentialMovingAverage] = None,
    device: torch.device = torch.device("cuda"),
) -> Tuple[float, float]:
    """"
    Args:
        num_eval_batches: If we aren't evaluating on the whole dataloader, then do on this many batches.
        ema: EMA object whose average_parameters() context is used for model evaluation. If no EMA, then this object should be None.
    Returns:
        accuracy: Average percentage accuracy on batch.
        loss: Average loss on batch.
    """
    model = model.to(device).eval()
    total_accuracy = 0.
    total_loss = 0.

    context_manager = ema.average_parameters() if ema else no_op_context()
    # Saving moving average weights in state dict
    with context_manager:
        for data in take_n(data_loader, num_eval_batches):
            inputs, labels = data["input_ids"].to(device), data["label_ids"].to(device)
            outputs = model(inputs)
        
            total_loss += F.cross_entropy(outputs, labels.long())
            # Second dimension is class dimension in PyTorch for sequence data (see AutoGPT transformer for details)
            probs = torch.softmax(outputs, dim=1)
            predictions = torch.argmax(probs, dim=1)

            correct_predictions = predictions == labels
            total_accuracy += correct_predictions.float().mean().item() * 100

    model.train()
    return total_accuracy / num_eval_batches, (total_loss / num_eval_batches).item()


def update_with_wandb_config(config: OmegaConf, sweep_params: list[str]) -> OmegaConf:
    """Check if each parameter exists in wandb.config and update it if it does."""
    for param in sweep_params:
        if param in wandb.config:
            print("Updating param with value from wandb config: ", param)
            OmegaConf.update(config, param, wandb.config[param], merge=True)
    return config


@contextlib.contextmanager
def no_op_context():
    yield