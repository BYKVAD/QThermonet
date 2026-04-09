# -*- coding: utf-8 -*-
"""
Created on Fri Mar 20 14:04:19 2026
Contains functions that should be reachable from any processing algorithm
@author: JANA
"""

# utils.py
import requests as rq

def fetch_api_data(X, Y):
    """
    Fetch raw API data for a given point.
    Returns data dict or raises a ValueError with a descriptive message.
    """
    
    # Check if coordinates are within Denmark's bounding box (EPSG:25832)
    DK_X_MIN = 441000
    DK_X_MAX = 894000
    DK_Y_MIN = 6048000
    DK_Y_MAX = 6403000

    if not (DK_X_MIN <= X <= DK_X_MAX and DK_Y_MIN <= Y <= DK_Y_MAX):
        raise ValueError(
            f"Coordinates (X={X}, Y={Y}) are outside Denmark. "
            f"Ground thermal conductivity cannot be estimated."
        )
        
    base_url = "https://data.geus.dk/geusmapmore/termiskejordarter/indexapimodel.jsp"
    params   = {"x": X, "y": Y}

    try:
        response = rq.get(base_url, params=params, timeout=10)
    except rq.exceptions.ConnectionError:
        raise ValueError("Could not connect to the GEUS API. Check your internet connection.")
    except rq.exceptions.Timeout:
        raise ValueError(f"The GEUS API request timed out for X={X}, Y={Y}.")
    except rq.exceptions.RequestException as e:
        raise ValueError(f"Unexpected error during API request: {str(e)}")

    if response.status_code != 200:
        raise ValueError(f"API returned status code {response.status_code} for X={X}, Y={Y}.")

    # Try to parse JSON
    try:
        data = response.json()
    except ValueError:
        raise ValueError(f"API returned invalid JSON for X={X}, Y={Y}: {response.text[:200]}")

    # Check for error key in response
    if "error" in data:
        raise ValueError(f"API returned an error for X={X}, Y={Y}: {data['error']}")

    # Check that expected keys are present
    for key in ["layers", "groundlevel", "phreatic"]:
        if key not in data:
            raise ValueError(f"API response missing expected key '{key}' for X={X}, Y={Y}.")

    return data

def calculate_tc(X, Y, depth):
    """
    Fetch API data and calculate weighted average thermal conductivity 
    for a single point with coordinates X,Y down to depth.
    Returns thermal conductivity value or None if the API call fails.
    """

    try:
        data = fetch_api_data(X, Y)
    except ValueError:
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
    
def get_representative_point(input_layer):
    """
    Extract a representative point from a vector layer in EPSG:25832.
    - Polygon: centroid
    - Line: midpoint
    - Point: point coordinates
    
    Returns (x, y) tuple in EPSG:25832 or raises an exception if input is invalid.
    """
    from qgis.core import (QgsVectorLayer, QgsWkbTypes, QgsCoordinateReferenceSystem,
                           QgsCoordinateTransform, QgsProject)

    if not input_layer or not isinstance(input_layer, QgsVectorLayer):
        raise ValueError("Invalid input layer!")

    if input_layer.geometryType() not in [
        QgsWkbTypes.PolygonGeometry,
        QgsWkbTypes.PointGeometry,
        QgsWkbTypes.LineGeometry
    ]:
        raise ValueError("Input layer must be a polygon, point or line!")

    features = list(input_layer.getFeatures())
    if len(features) != 1:
        raise ValueError("Input layer must contain exactly one feature!")

    feature = features[0]
    if not feature.isValid() or feature.geometry().isEmpty():
        raise ValueError("Input feature contains invalid or empty geometry!")

    # Reproject to EPSG:25832 if needed
    geometry   = feature.geometry()
    source_crs = input_layer.crs()
    target_crs = QgsCoordinateReferenceSystem("EPSG:25832")

    if source_crs != target_crs:
        transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance())
        geometry.transform(transform)

    # Extract representative point based on geometry type
    geom_type = input_layer.geometryType()

    if geom_type == QgsWkbTypes.PolygonGeometry:
        point = geometry.centroid().asPoint()
    elif geom_type == QgsWkbTypes.PointGeometry:
        point = geometry.asPoint()
    elif geom_type == QgsWkbTypes.LineGeometry:
        point = geometry.interpolate(geometry.length() / 2).asPoint()

    return round(point.x()), round(point.y())