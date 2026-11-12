"""Adaptive intelligence layer.

Implements the "adapt first, retrain last" philosophy of the adaptive AI architecture on top of the
physics-guided diagnostic engine:

    detect drift -> understand behaviour -> generate hypotheses -> estimate hidden state ->
    validate via digital twin -> optimise action -> recommend -> self-calibrate ->
    learn from experience -> retrain only if necessary

Modules
    drift       Layer 7  - continuous monitoring & drift detection (data, calibration, prediction,
                           sensor, regime drift and the Layer 1 data gate)
    experience  Layer 8  - engineering experience memory (verified cases -> priors, similar cases)
    twin        Layer 11 - digital-twin replay: hypotheses re-simulated through the nonlinear simulator
    controller  Layer 8/9/10 - the adaptive loop, bounded self-calibration with governance, retrain gate
"""
