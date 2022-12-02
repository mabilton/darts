"""
Data Transformer Base Class
---------------------------
"""

from abc import ABC, abstractmethod
from typing import (
    Any,
    Generator,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import numpy as np
import xarray as xr

from darts import TimeSeries
from darts.logging import get_logger, raise_if, raise_if_not, raise_log
from darts.utils import _build_tqdm_iterator, _parallel_apply

logger = get_logger(__name__)


class BaseDataTransformer(ABC):
    def __init__(
        self,
        name: str = "BaseDataTransformer",
        n_jobs: int = 1,
        verbose: bool = False,
        parallel_params: Union[bool, Sequence[str]] = False,
        mask_components: bool = True,
    ):
        """Abstract class for data transformers.

        All the deriving classes have to implement the static method :func:`ts_transform`.
        The class offers the method :func:`transform()`, for applying a transformation to a ``TimeSeries`` or
        ``Sequence[TimeSeries]``. This class takes care of parallelizing the transformation of multiple ``TimeSeries``
        when possible.

        Data transformers requiring to be fit first before calling :func:`transform` should derive
        from :class:`.FittableDataTransformer` instead.
        Data transformers that are invertible should derive from :class:`.InvertibleDataTransformer` instead.

        Note: the :func:`ts_transform` method is designed to be a static method instead of a instance method to allow an
        efficient parallelisation also when the scaler instance is storing a non-negligible amount of data. Using
        an instance method would imply copying the instance's data through multiple processes, which can easily
        introduce a bottleneck and nullify parallelisation benefits.

        Parameters
        ----------
        name
            The data transformer's name
        n_jobs
            The number of jobs to run in parallel. Parallel jobs are created only when a ``Sequence[TimeSeries]``
            is passed as input to a method, parallelising operations regarding different TimeSeries.
            Defaults to `1` (sequential). Setting the parameter to `-1` means using all the available processors.
            Note: for a small amount of data, the parallelisation overhead could end up increasing the total
            required amount of time.
        verbose
            Optionally, whether to print operations progress
        parallel_params
            Optionally, specifies which fixed parameters (i.e. the attributes initialized in the child-most
            class's `__init__`) take on different values for different parallel jobs. Fixed parameters specified
            by `parallel_params` are assumed to be a `Sequence` of values that should be used for that parameter
            in each parallel job; the length of this `Sequence` should equal the number of parallel jobs. If
            `parallel_params=True`, every fixed parameter will take on a different value for each
            parallel job. If `parallel_params=False`, every fixed parameter will take on the same value for
            each parallel job. If `parallel_params` is a `Sequence` of fixed attribute names, only those
            attribute names specified will take on different values between different parallel jobs.
        mask_components
            Optionally, whether or not to automatically apply any provided `component_mask`s to the
            `TimeSeries` inputs passed to `transform`, `fit`, `inverse_transform`, or `fit_transform`.
            If `True`, any specified `component_mask` will be applied to each input timeseries
            before passing them to the called method; the masked components will also be automatically
            'unmasked' in the returned `TimeSeries`. If `False`, then `component_mask` (if provided) will
            be passed as a keyword argument, but won't automatically be applied to the input timeseries.
            See `apply_component_mask` for further details.

        """
        # Assume `super().__init__` called at *very end* of
        # child-most class's `__init__`, so `vars(self)` contains
        # only attributes of this child-most class:
        self._fixed_params = vars(self).copy()
        # If `parallel_params = True`, but not a list of key values:
        if parallel_params and not isinstance(parallel_params, Sequence):
            parallel_params = self._fixed_params.keys()
        # If `parallel_params = False`:
        elif not parallel_params:
            parallel_params = tuple()
        self._parallel_params = parallel_params
        self._mask_components = mask_components
        self._name = name
        self._verbose = verbose
        self._n_jobs = n_jobs

    def set_verbose(self, value: bool):
        """Set the verbosity status.

        `True` for enabling the detailed report about scaler's operation progress,
        `False` for no additional information.

        Parameters
        ----------
        value
            New verbosity status
        """
        raise_if_not(isinstance(value, bool), "Verbosity status must be a boolean.")

        self._verbose = value

    def set_n_jobs(self, value: int):
        """Set the number of processors to be used by the transformer while processing multiple ``TimeSeries``.

        Parameters
        ----------
        value
            New n_jobs value.  Set to `-1` for using all the available cores.
        """

        raise_if_not(isinstance(value, int), "n_jobs must be an integer")
        self._n_jobs = value

    @staticmethod
    @abstractmethod
    def ts_transform(series: TimeSeries, params: Mapping[str, Any]) -> TimeSeries:
        """The function that will be applied to each series when :func:`transform()` is called.

        The function must take as first argument a ``TimeSeries`` object, and return the transformed ``TimeSeries``
        object. If more parameters are added as input in the derived classes, the ``_transform_iterator()`` should be
        redefined accordingly, to yield the necessary arguments to this function (See ``_transform_iterator()`` for
        further details).

        This method is not implemented in the base class and must be implemented in the deriving classes.

        Parameters
        ----------
        series
            series to be transformed.

        Notes
        -----
        This method is designed to be a static method instead of instance method to allow an efficient
        parallelisation also when the scaler instance is storing a non-negligible amount of data. Using instance
        methods would imply copying the instance's data through multiple processes, which can easily introduce a
        bottleneck and nullify parallelisation benefits.
        """
        pass

    def _transform_iterator(
        self, series: Sequence[TimeSeries]
    ) -> Iterator[Tuple[TimeSeries]]:
        """
        Return an ``Iterator`` object with tuples of inputs for each single call to :func:`ts_transform()`.
        Additional `args` and `kwargs` from :func:`transform()` (constant across all the calls to
        :func:`ts_transform()`) are already forwarded, and thus don't need to be included in this generator.

        The basic implementation of this method returns ``zip(series)``, i.e., a generator of single-valued tuples,
        each containing one ``TimeSeries`` object.

        Parameters
        ----------
        series
            Sequence of series received in input.

        Returns
        -------
        Iterator[Tuple[TimeSeries]]
            An iterator containing tuples of inputs for the :func:`ts_transform` method.

        Examples
        ________

        class IncreasingAdder(BaseDataTransformer):
            def __init__(self):
                super().__init__()

            @staticmethod
            def ts_transform(series: TimeSeries, n: int) -> TimeSeries:
                return series + n

            def _transform_iterator(self, series: Sequence[TimeSeries]) -> Iterator[Tuple[TimeSeries, int]]:
                return zip(series, (i for i in range(len(series))))

        """
        params = self._get_params(n_timeseries=len(series))
        return zip(series, params)

    def transform(
        self, series: Union[TimeSeries, Sequence[TimeSeries]], *args, **kwargs
    ) -> Union[TimeSeries, List[TimeSeries]]:
        """Transform a (sequence of) of series.

        In case a ``Sequence`` is passed as input data, this function takes care of
        parallelising the transformation of multiple series in the sequence at the same time.

        Parameters
        ----------
        series
            (sequence of) series to be transformed.
        args
            Additional positional arguments for each :func:`ts_transform()` method call
        kwargs
            Additional keyword arguments for each :func:`ts_transform()` method call

            component_mask : Optional[np.ndarray] = None
                Optionally, a 1-D boolean np.ndarray of length ``series.n_components`` that specifies which
                components of the underlying `series` the transform should consider.

        Returns
        -------
        Union[TimeSeries, List[TimeSeries]]
            Transformed data.
        """

        desc = f"Transform ({self._name})"

        # Take note of original input for unmasking purposes:
        if isinstance(series, TimeSeries):
            input_series = [series]
            data = [series]
        else:
            input_series = series
            data = series

        if self._mask_components:
            mask = kwargs.pop("component_mask", None)
            data = [self.apply_component_mask(ts, mask, return_ts=True) for ts in data]

        input_iterator = _build_tqdm_iterator(
            self._transform_iterator(data),
            verbose=self._verbose,
            desc=desc,
            total=len(data),
        )

        transformed_data = _parallel_apply(
            input_iterator, self.__class__.ts_transform, self._n_jobs, args, kwargs
        )

        if self._mask_components:
            unmasked = []
            for ts, transformed_ts in zip(input_series, transformed_data):
                unmasked.append(self.unapply_component_mask(ts, transformed_ts, mask))
            transformed_data = unmasked

        return (
            transformed_data[0] if isinstance(series, TimeSeries) else transformed_data
        )

    def _get_params(
        self, n_timeseries: int
    ) -> Generator[Mapping[str, Any], None, None]:
        """
        Creates generator of dictionaries containing fixed parameter values
        (i.e. attributes defined in the child-most class). Those fixed parameters
        specified by `parallel_params` are given different values over each of the
        parallel jobs. Called by `_transform_iterator` and `_inverse_transform_iterator`,
        if `Transformer` does *not* inherit from `FittableTransformer`.
        """
        self._check_fixed_params(n_timeseries)

        def params_generator(n_timeseries, fixed_params, parallel_params):
            fixed_params_copy = fixed_params.copy()
            for i in range(n_timeseries):
                for key in parallel_params:
                    fixed_params_copy[key] = fixed_params[key][i]
                if fixed_params_copy:
                    params = {"fixed": fixed_params_copy}
                else:
                    params = None
                yield params
            return None

        return params_generator(n_timeseries, self._fixed_params, self._parallel_params)

    def _check_fixed_params(self, n_timeseries: int) -> None:
        """
        Raises `ValueError` if `self._parallel_params` specifies a `key` in
        `self._fixed_params` that should be distributed, but
        `len(self._fixed_params[key])` does not equal `n_timeseries`.
        """
        for key in self._parallel_params:
            if len(self._fixed_params[key]) != n_timeseries:
                key = key[1:] if key[0] == "_" else key
                msg = (
                    f"{n_timeseries} TimeSeries were provided "
                    f"but only {len(self._fixed_params[key])} {key} values "
                    f"were specified upon initialising {self.name}."
                )
                raise_log(ValueError(msg))
        return None

    @staticmethod
    def apply_component_mask(
        series: TimeSeries, component_mask: Optional[np.ndarray] = None, return_ts=False
    ) -> np.ndarray:
        """
        Extracts components specified by `component_mask` from `series`

        Parameters
        ----------
        series
            input TimeSeries to be fed into transformer.
        component_mask
            Optionally, np.ndarray boolean mask of shape (n_components, 1) specifying which components to
            extract from `series`. The `i`th component of `series` is kept only if `component_mask[i] = True`.
            If not specified, no masking is performed.
        return_ts
            Optionally, specifies that a `TimeSeries` should be returned, rather than an `np.ndarray`.

        Returns
        -------
        masked
            `TimeSeries` (if `return_ts = True`) or `np.ndarray` (if `return_ts = True`) with only those components
            specified by `component_mask` remaining.

        """
        if component_mask is None:
            masked = series.copy() if return_ts else series.all_values()
        else:
            raise_if_not(
                isinstance(component_mask, np.ndarray) and component_mask.dtype == bool,
                f"`component_mask` must be a boolean `np.ndarray`, not a {type(component_mask)}.",
                logger,
            )
            raise_if_not(
                series.width == len(component_mask),
                "mismatch between number of components in `series` and length of `component_mask`",
                logger,
            )
            masked = series.all_values()[:, component_mask, :]
            if return_ts:
                # Remove masked components from coords:
                coords = dict(series._xa.coords)
                coords["component"] = coords["component"][component_mask]
                new_xa = xr.DataArray(
                    masked, dims=series._xa.dims, coords=coords, attrs=series._xa.attrs
                )
                masked = TimeSeries(new_xa)
        return masked

    @staticmethod
    def unapply_component_mask(
        series: TimeSeries,
        vals: Union[np.ndarray, TimeSeries],
        component_mask: Optional[np.ndarray] = None,
    ) -> Union[np.ndarray, TimeSeries]:
        """
        Adds back components previously removed by `component_mask` in `apply_component_mask` method.

        Parameters
        ----------
        series
            input TimeSeries that was fed into transformer.
        vals:
            `np.ndarray` or `TimeSeries` to 'unmask'
        component_mask
            Optionally, np.ndarray boolean mask of shape (n_components, 1) specifying which components were extracted
            from `series`. If given, insert `vals` back into the columns of the original array. If not specified,
            nothing is 'unmasked'.

        Returns
        -------
        unmasked
            `TimeSeries` (if `vals` is a `TimeSeries`) or `np.ndarray` (if `vals` is an `np.ndarray`) with those
            components previously removed by `component_mask` now 'added back'.
        """

        if component_mask is None:
            unmasked = vals
        else:
            raise_if_not(
                isinstance(component_mask, np.ndarray) and component_mask.dtype == bool,
                "If `component_mask` is given, must be a boolean np.ndarray`",
                logger,
            )
            raise_if_not(
                series.width == len(component_mask),
                "mismatch between number of components in `series` and length of `component_mask`",
                logger,
            )
            unmasked = series.all_values()
            if isinstance(vals, TimeSeries):
                unmasked[:, component_mask, :] = vals.all_values()
                # Remove timepoints not present in transformed data:
                unmasked = series.slice_intersect(vals).with_values(unmasked)
            else:
                unmasked[:, component_mask, :] = vals
        return unmasked

    @staticmethod
    def stack_samples(vals: Union[np.ndarray, TimeSeries]) -> np.ndarray:
        """
        Creates an array of shape `(n_timesteps * n_samples, n_components)` from
        either a `TimeSeries` or the `array_values` of a `TimeSeries`.

        Each column of the returned array corresponds to a component (dimension)
        of the series and is formed by concatenating all of the samples associated
        with that component together. More specifically, the `i`th column is formed
        by concatenating
        `[component_i_sample_1, component_i_sample_2, ..., component_i_sample_n]`.

        Stacking is useful when implementing a transformation that applies the exact same
        change to every timestep in the timeseries. In such cases, the samples of each
        component can be stacked together into a single column, and the transformation can then be
        applied to each column, thereby 'vectorising' the transformation over all samples of that component;
        the `unstack_samples` method can then be used to reshape the output. For transformations that depend
        on the `time_index` or the temporal ordering of the observations, stacking should *not* be employed.

        Parameters
        ----------
        vals
            `Timeseries` or `np.ndarray` of shape `(n_timesteps, n_components, n_samples)` to be 'stacked'.

        Returns
        -------
        stacked
            `np.ndarray` of shape `(n_timesteps * n_samples, n_components)`, where the `i`th column is formed
            by concatenating all of the samples of the `i`th component in `vals`.
        """
        if isinstance(vals, TimeSeries):
            vals = vals.all_values()
        shape = vals.shape
        new_shape = (shape[0] * shape[2], shape[1])
        stacked = np.swapaxes(vals, 1, 2).reshape(new_shape)
        return stacked

    @staticmethod
    def unstack_samples(
        vals: np.ndarray,
        n_timesteps: Optional[int] = None,
        n_samples: Optional[int] = None,
        series: Optional[TimeSeries] = None,
    ) -> np.ndarray:
        """
        Reshapes the 2D array returned by `stack_samples` back into an array of shape
        `(n_timesteps, n_components, n_samples)`; this 'undoes' the reshaping of
        `stack_samples`. Either `n_components`, `n_samples`, or `series` must be specified.

        Parameters
        ----------
        vals
            `np.ndarray` of shape `(n_timesteps * n_samples, n_components)` to be 'unstacked'.
        n_timesteps
            Optionally, the number of timesteps in the array originally passed to `stack_samples`.
            Does *not* need to be provided if `series` is specified.
        n_samples
            Optionally, the number of samples in the array originally passed to `stack_samples`.
            Does *not* need to be provided if `series` is specified.
        series
            Optionally, the `TimeSeries` object used to create `vals`; `n_samples` is inferred
            from this.

        Returns
        -------
        unstacked
            `np.ndarray` of shape `(n_timesteps, n_components, n_samples)`.

        """
        if series is not None:
            n_samples = series.n_samples
        else:
            raise_if(
                all(x is None for x in [n_timesteps, n_samples]),
                "Must specify either `n_timesteps`, `n_samples`, or `series`.",
            )
        n_components = vals.shape[-1]
        if n_timesteps is not None:
            reshaped_vals = vals.reshape(n_timesteps, -1, n_components)
        else:
            reshaped_vals = vals.reshape(-1, n_samples, n_components)
        unstacked = np.swapaxes(reshaped_vals, 1, 2)
        return unstacked

    @property
    def name(self):
        """Name of the data transformer."""
        return self._name

    def __str__(self):
        return self._name

    def __repr__(self):
        return self.__str__()
