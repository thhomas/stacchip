import json
import math
from multiprocessing import Pool
from pathlib import Path
from urllib.parse import urlparse

import boto3
import rasterio
from numpy.typing import ArrayLike
from pystac import Item
from rasterio.enums import Resampling
from rasterio.windows import Window

from stacchip.indexer import ChipIndexer

ASSET_BLACKLIST = ["scl", "qa_pixel"]


class Chipper:

    def __init__(
        self,
        platform: str,
        item_id: str,
        chip_index_x: int,
        chip_index_y: int,
        bucket: str = "",
        mountpath: str = "",
    ) -> None:
        if mountpath and bucket:
            raise ValueError("Specify either a bucket name or a mountpath")

        self.chip_index_x = chip_index_x
        self.chip_index_y = chip_index_y
        self.mountpath = Path(mountpath)
        self.is_remote = bool(bucket)

        if self.is_remote:
            self.indexer = self.load_indexer_s3(bucket, platform, item_id)
        else:
            self.indexer = self.load_indexer_local(mountpath, platform, item_id)

    def load_indexer_s3(self, bucket: str, platform: str, item_id: str) -> ChipIndexer:
        s3 = boto3.resource("s3")
        s3_bucket = s3.Bucket(name=bucket)
        content_object = s3_bucket.Object(f"{platform}/{item_id}/stac_item.json")
        file_content = content_object.get()["Body"].read().decode("utf-8")
        json_content = json.loads(file_content)
        item = Item.from_dict(json_content)

        return ChipIndexer(item)

    def load_indexer_local(
        self, mountpath: Path, platform: str, item_id: str
    ) -> ChipIndexer:
        item = Item.from_file(mountpath / Path(f"{platform}/{item_id}/stac_item.json"))
        return ChipIndexer(item)

    def get_pixels_for_asset(self, key: str) -> ArrayLike:

        asset = self.indexer.item.assets[key]

        srcpath = asset.href
        if not self.is_remote:
            url = urlparse(srcpath, allow_fragments=False)
            srcpath = self.mountpath / Path(url.path.lstrip("/"))

        with rasterio.open(srcpath) as src:
            # Currently assume that different assets may be at different
            # resolutions, but are aligned and the gsd differs by an integer
            # multiplier.
            if self.indexer.shape[0] % src.height:
                raise ValueError(
                    f"Asset height {src.height} is not a multiple of highest resolution height {self.indexer.shape[0]}"  # noqa: E501
                )

            if self.indexer.shape[1] % src.width:
                raise ValueError(
                    f"Asset width {src.width} is not a multiple of highest resolution height {self.indexer.shape[1]}"  # noqa: E501
                )

            factor = self.indexer.shape[0] / src.height
            if factor != 1:
                print(
                    f"Asset {asset.title} is not at highest resolution using scaling factor of {factor}"  # noqa: E501
                )

            chip_window = Window(
                math.floor(self.chip_index_x * self.indexer.chip_size / factor),
                math.floor(self.chip_index_y * self.indexer.chip_size / factor),
                math.ceil(self.indexer.chip_size / factor),
                math.ceil(self.indexer.chip_size / factor),
            )

            print(f"Chip window for asset {asset.title} is {chip_window}")
            return src.read(
                window=chip_window,
                out_shape=(src.count, self.indexer.chip_size, self.indexer.chip_size),
                resampling=Resampling.nearest,
            )

    @property
    def chip(self) -> dict:

        keys = [
            key for key in self.indexer.item.assets.keys() if key not in ASSET_BLACKLIST
        ]

        with Pool(len(keys)) as p:
            data = p.map(self.get_pixels_for_asset, keys)

        return dict(zip(keys, data))
