"""Keep the signing private key outside the checkout; package only its public key."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xiaoyu.update_protocol import validate_channel


def load_private_key(path: Path) -> Ed25519PrivateKey:
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("签名私钥必须保存在项目目录之外，不能进入代码或发行包。")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("请使用 Ed25519 签名私钥。")
    return key


def public_key_text(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode("ascii")


def configure_channel(private_key: Path, manifest_url: str, channel_path: Path,
                      *, generate: bool = False) -> dict:
    private_key = Path(private_key).resolve()
    if private_key.is_relative_to(ROOT):
        raise ValueError("签名私钥必须保存在项目目录之外。")
    if not private_key.exists():
        if not generate:
            raise ValueError("签名私钥不存在；首次配置请加 --generate。")
        key = Ed25519PrivateKey.generate()
        # Validate the URL before creating a key or configuration.
        validate_channel({"manifest_url": manifest_url, "public_key": public_key_text(key)})
        private_key.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            stream.flush()
            os.fsync(stream.fileno())
    key = load_private_key(private_key)
    channel = validate_channel({"manifest_url": manifest_url, "public_key": public_key_text(key)})
    channel_path = Path(channel_path)
    if channel_path.resolve() == private_key:
        raise ValueError("公钥配置不能覆盖签名私钥。")
    if channel_path.exists():
        existing = json.loads(channel_path.read_text(encoding="utf-8"))
        if existing != channel:
            raise ValueError("已有更新配置与本次不同，请先核对发布地址和原签名私钥。")
        return channel
    channel_path.parent.mkdir(parents=True, exist_ok=True)
    with channel_path.open("x", encoding="utf-8") as stream:
        json.dump(channel, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return channel


def main():
    parser = argparse.ArgumentParser(description="生成发布签名密钥并配置客户端更新地址。")
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--channel", type=Path, default=ROOT / "assets" / "update-channel.json")
    parser.add_argument("--generate", action="store_true", help="首次使用时创建私钥；已有私钥不会被替换")
    args = parser.parse_args()
    configure_channel(args.private_key, args.manifest_url, args.channel, generate=args.generate)
    print(f"更新公钥配置已保存：{args.channel}")
    print("请单独备份签名私钥；不要将它提交、上传或随程序分发。")


if __name__ == "__main__":
    main()
