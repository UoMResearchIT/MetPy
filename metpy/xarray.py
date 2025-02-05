# Copyright (c) 2018 MetPy Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
"""Provide accessors to enhance interoperability between XArray and MetPy."""
from __future__ import absolute_import

import functools
import logging
import re
import warnings

import xarray as xr

from ._vendor.xarray import either_dict_or_kwargs, expanded_indexer, is_dict_like
from .units import DimensionalityError, UndefinedUnitError, units

__all__ = []
readable_to_cf_axes = {'time': 'T', 'vertical': 'Z', 'y': 'Y', 'x': 'X'}
cf_to_readable_axes = {readable_to_cf_axes[key]: key for key in readable_to_cf_axes}

# Define the criteria for coordinate matches
coordinate_criteria = {
    'standard_name': {
        'time': 'time',
        'vertical': {'air_pressure', 'height', 'geopotential_height', 'altitude',
                     'model_level_number', 'atmosphere_ln_pressure_coordinate',
                     'atmosphere_sigma_coordinate',
                     'atmosphere_hybrid_sigma_pressure_coordinate',
                     'atmosphere_hybrid_height_coordinate', 'atmosphere_sleve_coordinate',
                     'height_above_geopotential_datum', 'height_above_reference_ellipsoid',
                     'height_above_mean_sea_level'},
        'y': 'projection_y_coordinate',
        'lat': 'latitude',
        'x': 'projection_x_coordinate',
        'lon': 'longitude'
    },
    '_CoordinateAxisType': {
        'time': 'Time',
        'vertical': {'GeoZ', 'Height', 'Pressure'},
        'y': 'GeoY',
        'lat': 'Lat',
        'x': 'GeoX',
        'lon': 'Lon'
    },
    'axis': readable_to_cf_axes,
    'positive': {
        'vertical': {'up', 'down'}
    },
    'units': {
        'vertical': {
            'match': 'dimensionality',
            'units': 'Pa'
        },
        'lat': {
            'match': 'name',
            'units': {'degree_north', 'degree_N', 'degreeN', 'degrees_north', 'degrees_N',
                      'degreesN'}
        },
        'lon': {
            'match': 'name',
            'units': {'degree_east', 'degree_E', 'degreeE', 'degrees_east', 'degrees_E',
                      'degreesE'}
        },
    },
    'regular_expression': {
        'time': r'time[0-9]*',
        'vertical': (r'(bottom_top|sigma|h(ei)?ght|altitude|depth|isobaric|pres|'
                     r'isotherm)[a-z_]*[0-9]*'),
        'y': r'y',
        'lat': r'x?lat[a-z0-9]*',
        'x': r'x',
        'lon': r'x?lon[a-z0-9]*'
    }
}

log = logging.getLogger(__name__)


@xr.register_dataarray_accessor('metpy')
class MetPyDataArrayAccessor(object):
    """Provide custom attributes and methods on XArray DataArray for MetPy functionality."""

    def __init__(self, data_array):
        """Initialize accessor with a DataArray."""
        self._data_array = data_array
        self._units = self._data_array.attrs.get('units', 'dimensionless')

    @property
    def units(self):
        if self._units != '%':
            return units(self._units)
        else:
            return units('percent')

    @property
    def unit_array(self):
        """Return data values as a `pint.Quantity`."""
        return self._data_array.values * self.units

    @unit_array.setter
    def unit_array(self, values):
        """Set data values as a `pint.Quantity`."""
        self._data_array.values = values
        self._units = self._data_array.attrs['units'] = str(values.units)

    def convert_units(self, units):
        """Convert the data values to different units in-place."""
        self.unit_array = self.unit_array.to(units)

    @property
    def crs(self):
        """Provide easy access to the `crs` coordinate."""
        if 'crs' in self._data_array.coords:
            return self._data_array.coords['crs'].item()
        raise AttributeError('crs attribute is not available.')

    @property
    def cartopy_crs(self):
        """Return the coordinate reference system (CRS) as a cartopy object."""
        return self.crs.to_cartopy()

    @property
    def cartopy_globe(self):
        """Return the globe belonging to the coordinate reference system (CRS)."""
        return self.crs.cartopy_globe

    def _fixup_coordinate_map(self, coord_map):
        """Ensure sure we have coordinate variables in map, not coordinate names."""
        for axis in coord_map:
            if coord_map[axis] is not None and not isinstance(coord_map[axis], xr.DataArray):
                coord_map[axis] = self._data_array[coord_map[axis]]

    def assign_coordinates(self, coordinates):
        """Assign the given coordinates to the given CF axis types."""
        if coordinates:
            # Assign the _metpy_axis attributes according to supplied mapping
            self._fixup_coordinate_map(coordinates)
            for axis in coordinates:
                if coordinates[axis] is not None:
                    coordinates[axis].attrs['_metpy_axis'] = axis
        else:
            # Clear _metpy_axis attribute on all coordinates
            for coord_var in self._data_array.coords.values():
                coord_var.attrs.pop('_metpy_axis', None)

    def _generate_coordinate_map(self):
        """Generate a coordinate map via CF conventions and other methods."""
        coords = self._data_array.coords.values()
        # Parse all the coordinates, attempting to identify x, y, vertical, time
        coord_lists = {'T': [], 'Z': [], 'Y': [], 'X': []}
        for coord_var in coords:

            # Identify the coordinate type using check_axis helper
            axes_to_check = {
                'T': ('time',),
                'Z': ('vertical',),
                'Y': ('y', 'lat'),
                'X': ('x', 'lon')
            }
            for axis_cf, axes_readable in axes_to_check.items():
                if check_axis(coord_var, *axes_readable):
                    coord_lists[axis_cf].append(coord_var)

        # Resolve any coordinate conflicts
        axis_conflicts = [axis for axis in coord_lists if len(coord_lists[axis]) > 1]
        for axis in axis_conflicts:
            self._resolve_axis_conflict(axis, coord_lists)

        # Collapse the coord_lists to a coord_map
        return {axis: (coord_lists[axis][0] if len(coord_lists[axis]) > 0 else None)
                for axis in coord_lists}

    def _resolve_axis_conflict(self, axis, coord_lists):
        """Handle axis conflicts if they arise."""
        if axis in ('Y', 'X'):
            # Horizontal coordinate, can be projection x/y or lon/lat. So, check for
            # existence of unique projection x/y (preferred over lon/lat) and use that if
            # it exists uniquely
            projection_coords = [coord_var for coord_var in coord_lists[axis] if
                                 check_axis(coord_var, 'x', 'y')]
            if len(projection_coords) == 1:
                coord_lists[axis] = projection_coords
                return

        # If one and only one of the possible axes is a dimension, use it
        dimension_coords = [coord_var for coord_var in coord_lists[axis] if
                            coord_var.name in coord_var.dims]
        if len(dimension_coords) == 1:
            coord_lists[axis] = dimension_coords
            return

        # Ambiguous axis, raise warning and do not parse
        warnings.warn('More than one ' + cf_to_readable_axes[axis] + ' coordinate present '
                      + 'for variable "' + self._data_array.name + '".')
        coord_lists[axis] = []

    def _metpy_axis_search(self, cf_axis):
        """Search for cached _metpy_axis attribute on the coordinates, otherwise parse."""
        # Search for coord with proper _metpy_axis
        coords = self._data_array.coords.values()
        for coord_var in coords:
            if coord_var.attrs.get('_metpy_axis') == cf_axis:
                return coord_var

        # Opportunistically parse all coordinates, and assign if not already assigned
        coord_map = self._generate_coordinate_map()
        for axis, coord_var in coord_map.items():
            if coord_var is not None and not any(coord.attrs.get('_metpy_axis') == axis
                                                 for coord in coords):
                coord_var.attrs['_metpy_axis'] = axis

        # Return parsed result (can be None if none found)
        return coord_map[cf_axis]

    def _axis(self, axis):
        """Return the coordinate variable corresponding to the given individual axis type."""
        if axis in readable_to_cf_axes:
            coord_var = self._metpy_axis_search(readable_to_cf_axes[axis])
            if coord_var is not None:
                return coord_var
            else:
                raise AttributeError(axis + ' attribute is not available.')
        else:
            raise AttributeError("'" + axis + "' is not an interpretable axis.")

    def coordinates(self, *args):
        """Return the coordinate variables corresponding to the given axes types."""
        for arg in args:
            yield self._axis(arg)

    @property
    def time(self):
        return self._axis('time')

    @property
    def vertical(self):
        return self._axis('vertical')

    @property
    def y(self):
        return self._axis('y')

    @property
    def x(self):
        return self._axis('x')

    def coordinates_identical(self, other):
        """Return whether or not the coordinates of other match this DataArray's."""
        # If the number of coordinates do not match, we know they can't match.
        if len(self._data_array.coords) != len(other.coords):
            return False

        # If same length, iterate over all of them and check
        for coord_name, coord_var in self._data_array.coords.items():
            if coord_name not in other.coords or not other[coord_name].identical(coord_var):
                return False

        # Otherwise, they match.
        return True

    def as_timestamp(self):
        """Return the data as unix timestamp (for easier time derivatives)."""
        attrs = {key: self._data_array.attrs[key] for key in
                 {'standard_name', 'long_name', 'axis', '_metpy_axis'}
                 & set(self._data_array.attrs)}
        attrs['units'] = 'seconds'
        return xr.DataArray(self._data_array.values.astype('datetime64[s]').astype('int'),
                            name=self._data_array.name,
                            coords=self._data_array.coords,
                            dims=self._data_array.dims,
                            attrs=attrs)

    def find_axis_name(self, axis):
        """Return the name of the axis corresponding to the given identifier.

        The given indentifer can be an axis number (integer), dimension coordinate name
        (string) or a standard axis type (string).
        """
        if isinstance(axis, int):
            # If an integer, use the corresponding dimension
            return self._data_array.dims[axis]
        elif axis not in self._data_array.dims and axis in readable_to_cf_axes:
            # If not a dimension name itself, but a valid axis type, get the name of the
            # coordinate corresponding to that axis type
            return self._axis(axis).name
        elif axis in self._data_array.dims and axis in self._data_array.coords:
            # If this is a dimension coordinate name, use it directly
            return axis
        else:
            # Otherwise, not valid
            raise ValueError('Given axis is not valid. Must be an axis number, a dimension '
                             'coordinate name, or a standard axis type.')

    class _LocIndexer(object):
        """Provide the unit-wrapped .loc indexer for data arrays."""

        def __init__(self, data_array):
            self.data_array = data_array

        def expand(self, key):
            """Parse key using xarray utils to ensure we have dimension names."""
            if not is_dict_like(key):
                labels = expanded_indexer(key, self.data_array.ndim)
                key = dict(zip(self.data_array.dims, labels))
            return key

        def __getitem__(self, key):
            key = _reassign_quantity_indexer(self.data_array, self.expand(key))
            return self.data_array.loc[key]

        def __setitem__(self, key, value):
            key = _reassign_quantity_indexer(self.data_array, self.expand(key))
            self.data_array.loc[key] = value

    @property
    def loc(self):
        """Make the LocIndexer available as a property."""
        return self._LocIndexer(self._data_array)

    def sel(self, indexers=None, method=None, tolerance=None, drop=False, **indexers_kwargs):
        """Wrap DataArray.sel to handle units."""
        indexers = either_dict_or_kwargs(indexers, indexers_kwargs, 'sel')
        indexers = _reassign_quantity_indexer(self._data_array, indexers)
        return self._data_array.sel(indexers, method=method, tolerance=tolerance, drop=drop)


@xr.register_dataset_accessor('metpy')
class MetPyDatasetAccessor(object):
    """Provide custom attributes and methods on XArray Dataset for MetPy functionality."""

    def __init__(self, dataset):
        """Initialize accessor with a Dataset."""
        self._dataset = dataset

    def parse_cf(self, varname=None, coordinates=None):
        """Parse Climate and Forecasting (CF) convention metadata.

        Parameters
        ----------
        varname : str or iterable of str, optional
            Name of the variable(s) to extract from the dataset while parsing for CF metadata.
            Defaults to all variables.
        coordinates : dict, optional
            Dictionary mapping CF axis types to coordinates of the variable(s). Only specify
            if you wish to override MetPy's automatic parsing of some axis type(s).

        Returns
        -------
        `xarray.DataArray` or `xarray.Dataset`
            Parsed DataArray (if varname is a string) or Dataset

        """
        from .cbook import is_string_like, iterable
        from .plots.mapping import CFProjection

        if varname is None:
            # If no varname is given, parse the entire dataset
            return self._dataset.apply(lambda da: self.parse_cf(da.name,
                                                                coordinates=coordinates),
                                       keep_attrs=True)
        elif iterable(varname) and not is_string_like(varname):
            # If non-string iterable is given, apply recursively across the varnames
            subset = xr.merge([self.parse_cf(single_varname, coordinates=coordinates)
                               for single_varname in varname])
            subset.attrs = self._dataset.attrs
            return subset

        var = self._dataset[varname]
        if 'grid_mapping' in var.attrs:
            proj_name = var.attrs['grid_mapping']
            try:
                proj_var = self._dataset.variables[proj_name]
            except KeyError:
                log.warning(
                    'Could not find variable corresponding to the value of '
                    'grid_mapping: {}'.format(proj_name))
            else:
                var.coords['crs'] = CFProjection(proj_var.attrs)

        self._fixup_coords(var)

        # Trying to guess whether we should be adding a crs to this variable's coordinates
        # First make sure it's missing CRS but isn't lat/lon itself
        if not check_axis(var, 'lat', 'lon') and 'crs' not in var.coords:
            # Look for both lat/lon in the coordinates
            has_lat = has_lon = False
            for coord_var in var.coords.values():
                has_lat = has_lat or check_axis(coord_var, 'lat')
                has_lon = has_lon or check_axis(coord_var, 'lon')

            # If we found them, create a lat/lon projection as the crs coord
            if has_lat and has_lon:
                var.coords['crs'] = CFProjection({'grid_mapping_name': 'latitude_longitude'})
                log.warning('Found lat/lon values, assuming latitude_longitude '
                            'for projection grid_mapping variable')

        # Assign coordinates if the coordinates argument is given
        if coordinates is not None:
            var.metpy.assign_coordinates(coordinates)

        return var

    def _fixup_coords(self, var):
        """Clean up the units on the coordinate variables."""
        for coord_name, data_array in var.coords.items():
            if (check_axis(data_array, 'x', 'y') and not check_axis(data_array, 'lon', 'lat')):
                try:
                    var.coords[coord_name].metpy.convert_units('meters')
                except DimensionalityError:  # Radians!
                    if 'crs' in var.coords:
                        new_data_array = data_array.copy()
                        height = var.coords['crs'].item()['perspective_point_height']
                        scaled_vals = new_data_array.metpy.unit_array * (height * units.meters)
                        new_data_array.metpy.unit_array = scaled_vals.to('meters')
                        var.coords[coord_name] = new_data_array

    class _LocIndexer(object):
        """Provide the unit-wrapped .loc indexer for datasets."""

        def __init__(self, dataset):
            self.dataset = dataset

        def __getitem__(self, key):
            parsed_key = _reassign_quantity_indexer(self.dataset, key)
            return self.dataset.loc[parsed_key]

    @property
    def loc(self):
        """Make the LocIndexer available as a property."""
        return self._LocIndexer(self._dataset)

    def sel(self, indexers=None, method=None, tolerance=None, drop=False, **indexers_kwargs):
        """Wrap Dataset.sel to handle units."""
        indexers = either_dict_or_kwargs(indexers, indexers_kwargs, 'sel')
        indexers = _reassign_quantity_indexer(self._dataset, indexers)
        return self._dataset.sel(indexers, method=method, tolerance=tolerance, drop=drop)


def check_axis(var, *axes):
    """Check if var satisfies the criteria for any of the given axes."""
    for axis in axes:
        # Check for
        #   - standard name (CF option)
        #   - _CoordinateAxisType (from THREDDS)
        #   - axis (CF option)
        #   - positive (CF standard for non-pressure vertical coordinate)
        for criterion in ('standard_name', '_CoordinateAxisType', 'axis', 'positive'):
            if (var.attrs.get(criterion, 'absent') in
                    coordinate_criteria[criterion].get(axis, set())):
                return True

        # Check for units, either by dimensionality or name
        try:
            if (axis in coordinate_criteria['units'] and (
                    (
                        coordinate_criteria['units'][axis]['match'] == 'dimensionality'
                        and (units.get_dimensionality(var.attrs.get('units'))
                             == units.get_dimensionality(
                                 coordinate_criteria['units'][axis]['units']))
                    ) or (
                        coordinate_criteria['units'][axis]['match'] == 'name'
                        and var.attrs.get('units')
                        in coordinate_criteria['units'][axis]['units']
                    ))):
                return True
        except UndefinedUnitError:
            pass

        # Check if name matches regular expression (non-CF failsafe)
        if re.match(coordinate_criteria['regular_expression'][axis], var.name.lower()):
            return True

    # If no match has been made, return False (rather than None)
    return False


def preprocess_xarray(func):
    """Decorate a function to convert all DataArray arguments to pint.Quantities.

    This uses the metpy xarray accessors to do the actual conversion.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        args = tuple(a.metpy.unit_array if isinstance(a, xr.DataArray) else a for a in args)
        kwargs = {name: (v.metpy.unit_array if isinstance(v, xr.DataArray) else v)
                  for name, v in kwargs.items()}
        return func(*args, **kwargs)
    return wrapper


def check_matching_coordinates(func):
    """Decorate a function to make sure all given DataArrays have matching coordinates."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        data_arrays = ([a for a in args if isinstance(a, xr.DataArray)]
                       + [a for a in kwargs.values() if isinstance(a, xr.DataArray)])
        if len(data_arrays) > 1:
            first = data_arrays[0]
            for other in data_arrays[1:]:
                if not first.metpy.coordinates_identical(other):
                    raise ValueError('Input DataArray arguments must be on same coordinates.')
        return func(*args, **kwargs)
    return wrapper


# If DatetimeAccessor does not have a strftime (xarray <0.12.2), monkey patch one in
try:
    from xarray.core.accessors import DatetimeAccessor
    if not hasattr(DatetimeAccessor, 'strftime'):
        def strftime(self, date_format):
            """Format time as a string."""
            import pandas as pd
            values = self._obj.data
            values_as_series = pd.Series(values.ravel())
            strs = values_as_series.dt.strftime(date_format)
            return strs.values.reshape(values.shape)

        DatetimeAccessor.strftime = strftime
except ImportError:
    pass


def _reassign_quantity_indexer(data, indexers):
    """Reassign a units.Quantity indexer to units of relevant coordinate."""
    def _to_magnitude(val, unit):
        try:
            return val.to(unit).m
        except AttributeError:
            return val

    for coord_name in indexers:
        # Handle axis types for DataArrays
        if (isinstance(data, xr.DataArray) and coord_name not in data.dims
                and coord_name in readable_to_cf_axes):
            axis = coord_name
            coord_name = next(data.metpy.coordinates(axis)).name
            indexers[coord_name] = indexers[axis]
            del indexers[axis]

        # Handle slices of quantities
        if isinstance(indexers[coord_name], slice):
            start = _to_magnitude(indexers[coord_name].start, data[coord_name].metpy.units)
            stop = _to_magnitude(indexers[coord_name].stop, data[coord_name].metpy.units)
            step = _to_magnitude(indexers[coord_name].step, data[coord_name].metpy.units)
            indexers[coord_name] = slice(start, stop, step)

        # Handle quantities
        indexers[coord_name] = _to_magnitude(indexers[coord_name],
                                             data[coord_name].metpy.units)

    return indexers
