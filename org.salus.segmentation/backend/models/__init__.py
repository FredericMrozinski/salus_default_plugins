"""Segmentation models for the model brushes (see modelbrush.py). Every module in this folder is imported when the
backend starts; a module registers its model, which then shows up as a mode in the tools.

A model gets the view around the brush, as seen on screen at the current zoom (1024 x 1024 x 3 uint8 RGB unless
`view=` says otherwise), and returns a map of the same width and height, either

  - edges (output="edges"): 1 where there is an edge, 0 elsewhere (or probabilities: above `threshold`, 0.5, is an
    edge). A dab fills what its centre reaches inside the brush circle without crossing an edge; gaps in the edges up to
    2 * `close` pixels wide (default close=1: 2 px) are closed first:

        # models/myedges.py
        import numpy as np
        from modelbrush import register

        _net = None

        def edges(rgb: np.ndarray) -> np.ndarray:
            global _net
            if _net is None:
                _net = load_my_network()  # once, on first use (this runs in a background thread)
            return _net(rgb)  # 1024 x 1024, 1 = edge

        register("myedges", "My Edges", edges, output="edges", about="Shown as the mode's tooltip")

  - labels (output="labels", the default): one region id per pixel, 0 = no region. A dab fills, inside the brush
    circle, the region under its centre:

        register("myregions", "My Regions", regions)

Weights and other large files do not belong in git; keep them next to the module and list them in a .gitignore here.
"""
