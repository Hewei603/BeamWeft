"""Show the native Save As dialog on the main thread of a short-lived process."""
import sys
from pathlib import Path
from tkinter import Tk, filedialog


def choose(source):
    window = Tk()
    window.withdraw()
    try:
        return filedialog.asksaveasfilename(
            parent=window, title="工程另存为", initialdir=str(source.parent), initialfile=source.with_suffix(".beamweft").name,
            defaultextension=".beamweft", filetypes=[("BeamWeft 工程", "*.beamweft")], confirmoverwrite=True,
        )
    finally:
        window.destroy()


if __name__ == "__main__":
    print(choose(Path(sys.argv[1])))
