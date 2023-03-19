import copy
import logging
from typing import Optional, Tuple, Union

import humanfriendly
import torch
from typeguard import check_argument_types

from espnet2.asr.frontend.abs_frontend import AbsFrontend
from espnet2.asr.frontend.adapter_utils.add_adapters import add_adapters_wav2vec2
from espnet2.utils.get_default_kwargs import get_default_kwargs
from espnet.nets.pytorch_backend.frontends.frontend import Frontend


class S3prlFrontend(AbsFrontend):
    """Speech Pretrained Representation frontend structure for ASR."""

    def __init__(
        self,
        fs: Union[int, str] = 16000,
        frontend_conf: Optional[dict] = get_default_kwargs(Frontend),
        download_dir: str = None,
        multilayer_feature: bool = False,
        layer: int = -1,
    ):
        try:
            import s3prl
            from s3prl.nn import Featurizer, S3PRLUpstream
        except Exception as e:
            print("Error: S3PRL is not properly installed.")
            print("Please install S3PRL: cd ${MAIN_ROOT}/tools && make s3prl.done")
            raise e

        assert check_argument_types()
        super().__init__()

        if isinstance(fs, str):
            fs = humanfriendly.parse_size(fs)
        if fs != 16000:
            logging.warning(
                "All the upstream models in S3PRL now only support 16 kHz audio."
            )

        if download_dir is not None:
            s3prl.util.download.set_dir(download_dir)

        assert frontend_conf.get("upstream", None) in S3PRLUpstream.available_names()
        upstream = S3PRLUpstream(
            frontend_conf.get("upstream"),
            path_or_url=frontend_conf.get("path_or_url", None),
            normalize=frontend_conf.get("normalize", False),
            extra_conf=frontend_conf.get("extra_conf", None),
        )
        upstream.eval()
        if getattr(
            upstream.upstream, "model", None
        ) is not None and upstream.upstream.model.__class__.__name__ in [
            "Wav2Vec2Model",
            "HubertModel",
        ]:
            upstream.upstream.model.encoder.layerdrop = 0.0

        # check if adapter is added
        if (
            upstream.upstream.model.__class__.__name__ == "Wav2Vec2Model"
            and frontend_conf.get("add_adapters", None)
        ):
            upstream.upstream = add_adapters_wav2vec2(
                upstream.upstream,
                adapter_down_dim=192,
                adapt_layers=frontend_conf.get("adapter_layers", None),
            )
        elif (
            upstream.upstream.model.__class__.__name__ == "Wav2Vec2Model"
            and frontend_conf.get("adapter_find", None)
        ):
            multilayer_feature = True
            self.adapter_num = frontend_conf.get("adapter_num", None)

        if layer != -1:
            layer_selections = [layer]
            assert (
                not multilayer_feature
            ), "multilayer feature will be deactivated, when specific layer used"
        else:
            layer_selections = None
        featurizer = Featurizer(upstream, layer_selections=layer_selections)

        self.multilayer_feature = multilayer_feature
        self.layer = layer
        self.upstream, self.featurizer = upstream, featurizer
        self.pretrained_params = copy.deepcopy(self.upstream.state_dict())
        self.frontend_type = "s3prl"
        self.hop_length = self.featurizer.downsample_rate
        self.tile_factor = frontend_conf.get("tile_factor", 1)

    def _tile_representations(self, feature):
        """Tile up the representations by `tile_factor`.

        Input - sequence of representations
                shape: (batch_size, seq_len, feature_dim)

        Output - sequence of tiled representations
                 shape: (batch_size, seq_len * factor, feature_dim)
        """
        assert (
            len(feature.shape) == 3
        ), "Input argument `feature` has invalid shape: {}".format(feature.shape)
        tiled_feature = feature.repeat(1, 1, self.tile_factor)
        tiled_feature = tiled_feature.reshape(
            feature.size(0), feature.size(1) * self.tile_factor, feature.size(2)
        )
        return tiled_feature

    def output_size(self) -> int:
        return self.featurizer.output_size

    def forward(
        self, input: torch.Tensor, input_lengths: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        feats, feats_lens = self.upstream(input, input_lengths)
        if self.layer != -1:
            layer = self.layer
            feats, feats_lens = feats[layer], feats_lens[layer]
            return feats, feats_lens

        if self.multilayer_feature:
            feats, feats_lens = self.featurizer(feats, feats_lens)
        else:
            feats, feats_lens = self.featurizer(feats[-1:], feats_lens[-1:])

        if self.tile_factor != 1:
            feats = self._tile_representations(feats)

        return feats, feats_lens

    def reload_pretrained_parameters(self):
        self.upstream.load_state_dict(self.pretrained_params)
        logging.info("Pretrained S3PRL frontend model parameters reloaded!")
