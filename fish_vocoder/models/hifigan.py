from typing import Any, Callable

import torch
import torch.nn.functional as F
from torch import nn

from fish_vocoder.models.vocoder import VocoderModel
from fish_vocoder.modules.losses.stft import MultiResolutionSTFTLoss
from fish_vocoder.utils.mask import sequence_mask


class HiFiGANModel(VocoderModel):
    def __init__(
        self,
        sampling_rate: int,
        n_fft: int,
        hop_length: int,
        win_length: int,
        num_mels: int,
        optimizer: Callable,
        lr_scheduler: Callable,
        mel_transforms: nn.ModuleDict,
        generator: nn.Module,
        discriminators: nn.ModuleDict,
        multi_resolution_stft_loss: MultiResolutionSTFTLoss,
        crop_length: int | None = None,
    ):
        super().__init__(
            sampling_rate=sampling_rate,
            n_fft=n_fft,
            hop_length=hop_length,
            win_length=win_length,
            num_mels=num_mels,
        )

        # Model parameters
        self.optimizer_builder = optimizer
        self.lr_scheduler_builder = lr_scheduler

        # Spectrogram transforms
        self.mel_transforms = mel_transforms

        # Generator and discriminators
        self.generator = generator
        self.discriminators = discriminators

        # Loss
        self.multi_resolution_stft_loss = multi_resolution_stft_loss

        # Crop length for saving memory
        self.crop_length = crop_length

        # Disable automatic optimization
        self.automatic_optimization = False

    def configure_optimizers(self):
        # Need two optimizers and two schedulers
        optimizer_generator = self.optimizer_builder(self.generator.parameters())
        optimizer_discriminator = self.optimizer_builder(
            self.discriminators.parameters()
        )

        lr_scheduler_generator = self.lr_scheduler_builder(optimizer_generator)
        lr_scheduler_discriminator = self.lr_scheduler_builder(optimizer_discriminator)

        return [optimizer_generator, optimizer_discriminator], [
            {
                "scheduler": lr_scheduler_generator,
                "interval": "step",
            },
            {
                "scheduler": lr_scheduler_discriminator,
                "interval": "step",
            },
        ]

    def training_step(self, batch, batch_idx):
        optim_g, optim_d = self.optimizers()

        audio, lengths = batch["audio"], batch["lengths"]
        audio_mask = sequence_mask(lengths)[:, None, :].to(audio.device, torch.float32)

        # Generator
        optim_g.zero_grad()
        input_mels = self.mel_transforms.input(audio.squeeze(1))
        fake_audio = self.generator(input_mels)

        assert fake_audio.shape == audio.shape

        # Apply mask
        audio = audio * audio_mask
        fake_audio = fake_audio * audio_mask

        # Multi-Resolution STFT Loss
        sc_loss, mag_loss = self.multi_resolution_stft_loss(
            fake_audio.squeeze(1), audio.squeeze(1)
        )
        loss_stft = sc_loss + mag_loss

        self.log(
            "train/generator/stft",
            loss_stft,
            on_step=True,
            on_epoch=False,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        # L1 Mel-Spectrogram Loss
        # This is not used in backpropagation currently
        with torch.no_grad():
            audio_mel = self.mel_transforms.loss(audio.squeeze(1))
            fake_audio_mel = self.mel_transforms.loss(fake_audio.squeeze(1))
            loss_mel = F.l1_loss(audio_mel, fake_audio_mel)

            self.log(
                "train/generator/mel",
                loss_mel,
                on_step=True,
                on_epoch=False,
                prog_bar=True,
                logger=True,
                sync_dist=True,
            )

        # Now, we need to reduce the length of the audio to save memory
        if self.crop_length is not None and audio.shape[2] > self.crop_length:
            slice_idx = torch.randint(0, audio.shape[-1] - self.crop_length, (1,))

            audio = audio[..., slice_idx : slice_idx + self.crop_length]
            fake_audio = fake_audio[..., slice_idx : slice_idx + self.crop_length]
            audio_mask = audio_mask[..., slice_idx : slice_idx + self.crop_length]

            assert audio.shape == fake_audio.shape == audio_mask.shape

        # Adv Loss
        loss_adv_all = 0

        for key, disc in self.discriminators.items():
            score_fake, _ = disc(fake_audio)
            loss_fake = torch.mean((1 - score_fake) ** 2)

            self.log(
                f"train/generator/adv_{key}",
                loss_fake,
                on_step=True,
                on_epoch=False,
                prog_bar=False,
                logger=True,
                sync_dist=True,
            )

            loss_adv_all += loss_fake

        loss_adv_all /= len(self.discriminators)
        loss_gen_all = loss_stft * 2.5 + loss_adv_all

        self.manual_backward(loss_gen_all)
        optim_g.step()

        # Discriminator
        optim_d.zero_grad()

        loss_disc_all = 0
        for key, disc in self.discriminators.items():
            score, _ = disc(audio)
            score_fake, _ = disc(fake_audio.detach())

            loss_disc = torch.mean((score - 1) ** 2) + torch.mean((score_fake) ** 2)

            self.log(
                f"train/discriminator/{key}",
                loss_disc,
                on_step=True,
                on_epoch=False,
                prog_bar=False,
                logger=True,
                sync_dist=True,
            )

            loss_disc_all += loss_disc

        loss_disc_all /= len(self.discriminators)

        self.manual_backward(loss_disc_all)
        optim_d.step()

        self.log(
            "train/discriminator/all",
            loss_disc_all,
            on_step=True,
            on_epoch=False,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        # Manual LR Scheduler
        scheduler_g, scheduler_d = self.lr_schedulers()
        scheduler_g.step()
        scheduler_d.step()

    def validation_step(self, batch: Any, batch_idx: int):
        y, lengths = batch["audio"], batch["lengths"]
        y_mask = sequence_mask(lengths)[:, None, :].to(y.device, torch.float32)
        input_mels = self.mel_transforms.input(y.squeeze(1))
        y_g_hat = self.generator(input_mels)

        assert y_g_hat.shape == y.shape

        # Apply mask
        y = y * y_mask
        y_g_hat = y_g_hat * y_mask

        # L1 Mel-Spectrogram Loss
        y_mel = self.mel_transforms.loss(y.squeeze(1))
        y_g_hat_mel = self.mel_transforms.loss(y_g_hat.squeeze(1))
        loss_mel = F.l1_loss(y_mel, y_g_hat_mel)

        self.log(
            "val/metrics/mel",
            loss_mel,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
            sync_dist=True,
        )

        # Report other metrics
        self.report_val_metrics(y_g_hat, y, lengths)

    @staticmethod
    def discriminator_loss(disc_real_outputs, disc_generated_outputs):
        loss = 0
        r_losses = []
        g_losses = []

        for dr, dg in zip(disc_real_outputs, disc_generated_outputs):
            r_loss = torch.mean((1 - dr) ** 2)
            g_loss = torch.mean(dg**2)

            loss += r_loss + g_loss
            r_losses.append(r_loss.item())
            g_losses.append(g_loss.item())

        return loss, r_losses, g_losses

    @staticmethod
    def generator_loss(disc_outputs):
        loss = 0
        losses = []

        for dg in disc_outputs:
            temp = torch.mean((1 - dg) ** 2)
            losses.append(temp)
            loss += temp

        return loss, losses

    @staticmethod
    def feature_matching_loss(disc_real_outputs, disc_generated_outputs):
        losses = []

        for dr, dg in zip(disc_real_outputs, disc_generated_outputs):
            for rl, gl in zip(dr, dg):
                losses.append(F.l1_loss(rl, gl))

        return sum(losses)
