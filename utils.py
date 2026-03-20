# -*- coding: utf-8 -*-
"""
Created on Fri Mar 20 14:04:19 2026
Contains functions that should be reachable from any processing algorithm
@author: JANA
"""

# utils.py
import requests as rq

def calculate_tc(X, Y, depth):
    """
    Fetch API data and calculate weighted average thermal conductivity 
    for a single point with coordinates X,Y down to depth.
    Returns thermal conductivity value or None if the API call fails.
    """

    base_url = "https://data.geus.dk/geusmapmore/termiskejordarter/indexapimodel.jsp"
    params   = {"x": X, "y": Y}

    response = rq.get(base_url, params=params)

    if response.status_code != 200:
        return None

    data = response.json()

    if "error" in data:
        return None

    if depth == 150:
        return data["tc_avg_0m_150m"]

    layers = data["layers"]

    tc_weighted_sum = 0
    total_thickness = 0

    for layer in layers:
        layer_top    = layer["top"]
        layer_bottom = layer["bottom"]

        if layer_bottom <= 0 or layer_top >= depth:
            continue

        clipped_top    = max(layer_top,    0)
        clipped_bottom = min(layer_bottom, depth)

        thickness_within_depth = clipped_bottom - clipped_top

        if thickness_within_depth > 0:
            tc_weighted_sum += layer["tc_corrected_for_phreatic"] * thickness_within_depth
            total_thickness += thickness_within_depth

    if total_thickness > 0:
        return tc_weighted_sum / total_thickness
    else:
        return None