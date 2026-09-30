"""Compile every *.wat in this directory to *.wasm using the Wasmtime API
that is already installed (no external toolchain required)."""

from pathlib import Path

from wasmtime import wat2wasm

HERE = Path(__file__).parent


def main() -> None:
    for wat in sorted(HERE.glob("*.wat")):
        wasm = wat2wasm(wat.read_bytes())
        out = wat.with_suffix(".wasm")
        out.write_bytes(wasm)
        print(f"built {out.name} ({len(wasm)} bytes)")


if __name__ == "__main__":
    main()
