"""Generate the Python gRPC code for the SDK from proto/engine.proto.

grpc_tools writes `import engine_pb2`, which only works for a top-level
module. The SDK is a package, so this script makes that import relative.

Usage: python tools/gen_proto.py
"""
import pathlib
import sys

from grpc_tools import protoc

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "sdk" / "capitol_sdk"


def main() -> int:
    code = protoc.main([
        "protoc",
        f"-I{ROOT / 'proto'}",
        f"--python_out={OUT}",
        f"--grpc_python_out={OUT}",
        f"--pyi_out={OUT}",
        str(ROOT / "proto" / "engine.proto"),
    ])
    if code != 0:
        return code
    grpc_file = OUT / "engine_pb2_grpc.py"
    text = grpc_file.read_text()
    grpc_file.write_text(text.replace("\nimport engine_pb2 as", "\nfrom . import engine_pb2 as"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
