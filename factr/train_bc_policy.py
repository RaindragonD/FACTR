# Copyright (c) Sudeep Dasari, 2023

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import os
import traceback

import numpy as np
import torch
import tqdm
from omegaconf import DictConfig, OmegaConf
import yaml

import hydra
from factr import misc, transforms
from copy import deepcopy
from pathlib import Path

base_path = os.path.dirname(os.path.abspath(__file__))


@hydra.main(config_path="cfg", config_name="train_bc.yaml")
def train_bc(cfg: DictConfig):
    try:
        resume_model = misc.init_job(cfg)

        # set random seeds for reproducibility
        torch.manual_seed(cfg.seed)
        np.random.seed(cfg.seed + 1)

        # save rollout config
        rollout_dir = Path("rollout")
        if not rollout_dir.exists():
            rollout_dir.mkdir()
            with open(rollout_dir / "agent_config.yaml", "w") as f:
                inference_config = deepcopy(cfg.agent)
                inference_config.features.restore_path = ''
                agent_yaml = OmegaConf.to_yaml(inference_config, resolve=True)
                f.write(agent_yaml)
            with open(rollout_dir / "exp_config.yaml", "w") as f:
                exp_yaml = OmegaConf.to_yaml(cfg)
                f.write(exp_yaml)
            rollout_config = OmegaConf.load(Path(cfg.buffer_path).parent / "rollout_config.yaml")
            with open(rollout_dir / "rollout_config.yaml", "w") as f:
                OmegaConf.save(rollout_config, f)

        # build agent from hydra configs
        agent = hydra.utils.instantiate(cfg.agent)
        trainer = hydra.utils.instantiate(cfg.trainer, model=agent, device_id=0)

        # build task, replay buffer, and dataloader
        task = hydra.utils.instantiate(
            cfg.task, batch_size=cfg.batch_size, num_workers=cfg.num_workers
        )

        # create a gpu train transform (if used)
        gpu_transform = (
            transforms.get_gpu_transform_by_name(cfg.train_transform)
            if "gpu" in cfg.train_transform
            else None
        )

        # restore/save the model as required
        if resume_model is not None:
            misc.GLOBAL_STEP = trainer.load_checkpoint(resume_model)
        elif misc.GLOBAL_STEP == 0:
            trainer.save_checkpoint(misc.GLOBAL_STEP)
        assert misc.GLOBAL_STEP >= 0, "GLOBAL_STEP not loaded correctly!"

        # register checkpoint handler and enter train loop
        misc.set_checkpoint_handler(trainer)
        print(f"Starting at Global Step {misc.GLOBAL_STEP}")

        trainer.set_train()
        train_iterator = iter(task.train_loader)
        for itr in (
            pbar := tqdm.tqdm(range(cfg.max_iterations), postfix=dict(Loss=None))
        ):
            if itr < misc.GLOBAL_STEP:
                continue

            # infinitely sample batches until the train loop is finished
            try:
                batch = next(train_iterator)
            except StopIteration:
                train_iterator = iter(task.train_loader)
                batch = next(train_iterator)

            # handle the image transform on GPU if specified
            if gpu_transform is not None:
                (imgs, obs), actions, mask = batch
                imgs = {k: v.to(trainer.device_id) for k, v in imgs.items()}
                imgs = {k: gpu_transform(v) for k, v in imgs.items()}
                batch = ((imgs, obs), actions, mask)
        
            trainer.optim.zero_grad()
            loss = trainer.training_step(batch, misc.GLOBAL_STEP)
            loss.backward()
            trainer.optim.step()

            pbar.set_postfix(dict(Loss=loss.item()))
            misc.GLOBAL_STEP += 1

            if misc.GLOBAL_STEP % cfg.schedule_freq == 0:
                trainer.step_schedule()

            if misc.GLOBAL_STEP % cfg.eval_freq == 0:
                trainer.set_eval()
                task.eval(trainer, misc.GLOBAL_STEP)
                trainer.set_train()

            if misc.GLOBAL_STEP >= cfg.max_iterations:
                trainer.save_checkpoint(misc.GLOBAL_STEP)
                return
            elif misc.GLOBAL_STEP % cfg.save_freq == 0:
                trainer.save_checkpoint(misc.GLOBAL_STEP)

    # gracefully handle and log errors
    except Exception:
        traceback.print_exc(file=open("exception.log", "w"))
        with open("exception.log", "r") as f:
            print(f.read())


if __name__ == "__main__":
    train_bc()
