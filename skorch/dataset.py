"""Contains custom skorch Dataset and CVSplit."""

from functools import partial
from numbers import Number

import numpy as np
from sklearn.model_selection import ShuffleSplit
from sklearn.model_selection import StratifiedKFold
from sklearn.model_selection import StratifiedShuffleSplit
from sklearn.model_selection import check_cv
import torch
import torch.utils.data

from skorch.utils import flatten
from skorch.utils import is_pandas_ndframe
from skorch.utils import multi_indexing
from skorch.utils import to_numpy
from skorch.utils import to_tensor


def _apply_to_data(data, func, unpack_dict=False):
    """Apply a function to data, trying to unpack different data
    types.

    """
    apply_ = partial(_apply_to_data, func=func, unpack_dict=unpack_dict)
    if isinstance(data, dict):
        if unpack_dict:
            return [apply_(v) for v in data.values()]
        return {k: apply_(v) for k, v in data.items()}
    elif isinstance(data, (list, tuple)):
        try:
            # e.g.list/tuple of arrays
            return [apply_(x) for x in data]
        except TypeError:
            return func(data)
    return func(data)


def get_len(data):
    lens = [_apply_to_data(data, len, unpack_dict=True)]
    lens = list(flatten(lens))
    len_set = set(lens)
    if len(len_set) != 1:
        raise ValueError("Dataset does not have consistent lengths.")
    return list(len_set)[0]


class Dataset(torch.utils.data.Dataset):
    """General dataset wrapper that can be used in conjunction with
    PyTorch :class:`~torch.utils.data.DataLoader`.

    The dataset will always yield a tuple of two values, the first
    from the data (``X``) and the second from the target (``y``).
    However, the target is allowed to be ``None``. In that case,
    :class:`.Dataset` will currently return a dummy tensor, since
    :class:`~torch.utils.data.DataLoader` does not work with
    ``None``\s.

    :class:`.Dataset` currently works with the following data types:

    * numpy ``array``\s
    * PyTorch :class:`~torch.Tensor`\s
    * pandas NDFrame
    * a dictionary of the former three
    * a list/tuple of the former three

    Parameters
    ----------
    X : see above
      Everything pertaining to the input data.

    y : see above or None (default=None)
      Everything pertaining to the target, if there is anything.

    device : str, torch.device (default='cpu')
      Which computation device to use (e.g., 'cuda').

    length : int or None (default=None)
      If not None, determines the length (``len``) of the data. Should
      usually be left at None, in which case the length is determined
      by the data itself.

    """
    def __init__(
            self,
            X,
            y=None,
            device='cpu',
            length=None,
    ):
        self.X = X
        self.y = y
        self.device = device

        if length is not None:
            self._len = length
            return

        # pylint: disable=invalid-name
        len_X = get_len(X)
        if y is not None:
            len_y = get_len(y)
            if len_y != len_X:
                raise ValueError("X and y have inconsistent lengths.")
        self._len = len_X

    def __len__(self):
        return self._len

    def transform(self, X, y):
        """Additional transformations on X and y.

        By default, they are cast to torch tensors. Override this if
        you want a different behavior.

        Note: If you use this in conjuction with PyTorch
        :class:`~torch.utils.data.DataLoader`, the latter will call
        the dataset for each row separately, which means that the
        incoming X and y each are single rows.

        """
        # pytorch DataLoader cannot deal with None so we use 0 as a
        # placeholder value. We only return a Tensor with one value
        # (as opposed to ``batchsz`` values) since the pytorch
        # DataLoader calls __getitem__ for each row in the batch
        # anyway, which results in a dummy ``y`` value for each row in
        # the batch.
        # FIXME:  BatchScoring and EpochScoring with caching will get
        # FIXME:: this placeholder value as `y` since they are operating
        # FIXME:: on batch level. This might be easy to trip over, esp.
        # FIXME:: since this value may look meaningful.
        y = torch.Tensor([0]) if y is None else y

        return (
            to_tensor(X, device=self.device),
            to_tensor(y, device=self.device),
        )

    def __getitem__(self, i):
        X, y = self.X, self.y
        if is_pandas_ndframe(X):
            X = {k: X[k].values.reshape(-1, 1) for k in X}

        Xi = multi_indexing(X, i)
        yi = y if y is None else multi_indexing(y, i)
        return self.transform(Xi, yi)


class CVSplit(object):
    """Class that performs the internal train/valid split on a dataset.

    The ``cv`` argument here works similarly to the regular sklearn ``cv``
    parameter in, e.g., ``GridSearchCV``. However, instead of cycling
    through all splits, only one fixed split (the first one) is
    used. To get a full cycle through the splits, don't use
    ``NeuralNet``'s internal validation but instead the corresponding
    sklearn functions (e.g. ``cross_val_score``).

    We additionally support a float, similar to sklearn's
    ``train_test_split``.

    Parameters
    ----------
    cv : int, float, cross-validation generator or an iterable, optional
      (Refer sklearn's User Guide for cross_validation for the various
      cross-validation strategies that can be used here.)

      Determines the cross-validation splitting strategy.
      Possible inputs for cv are:

        - None, to use the default 3-fold cross validation,
        - integer, to specify the number of folds in a ``(Stratified)KFold``,
        - float, to represent the proportion of the dataset to include
          in the validation split.
        - An object to be used as a cross-validation generator.
        - An iterable yielding train, validation splits.

    stratified : bool (default=False)
      Whether the split should be stratified. Only works if ``y`` is
      either binary or multiclass classification.

    random_state : int, RandomState instance, or None (default=None)
      Control the random state in case that ``(Stratified)ShuffleSplit``
      is used (which is when a float is passed to ``cv``). For more
      information, look at the sklearn documentation of
      ``(Stratified)ShuffleSplit``.

    """
    def __init__(
            self,
            cv=5,
            stratified=False,
            random_state=None,
    ):
        self.stratified = stratified
        self.random_state = random_state

        if isinstance(cv, Number) and (cv <= 0):
            raise ValueError("Numbers less than 0 are not allowed for cv "
                             "but CVSplit got {}".format(cv))
        self.cv = cv

    def _is_stratified(self, cv):
        return isinstance(cv, (StratifiedKFold, StratifiedShuffleSplit))

    def _is_float(self, x):
        if not isinstance(x, Number):
            return False
        return not float(x).is_integer()

    def _check_cv_float(self):
        cv_cls = StratifiedShuffleSplit if self.stratified else ShuffleSplit
        return cv_cls(test_size=self.cv, random_state=self.random_state)

    def _check_cv_non_float(self, y):
        return check_cv(
            self.cv,
            y=y,
            classifier=self.stratified,
        )

    def check_cv(self, y):
        """Resolve which cross validation strategy is used."""
        y_arr = None
        if self.stratified:
            # Try to convert y to numpy for sklearn's check_cv; if conversion
            # doesn't work, still try.
            try:
                y_arr = to_numpy(y)
            except (AttributeError, TypeError):
                y_arr = y

        if self._is_float(self.cv):
            return self._check_cv_float()
        return self._check_cv_non_float(y_arr)

    def _is_regular(self, x):
        return (x is None) or isinstance(x, np.ndarray) or is_pandas_ndframe(x)

    def __call__(self, dataset, y=None, groups=None):
        bad_y_error = ValueError(
            "Stratified CV requires explicitely passing a suitable y.")
        if (y is None) and self.stratified:
            raise bad_y_error

        cv = self.check_cv(y)
        if self.stratified and not self._is_stratified(cv):
            raise bad_y_error

        # pylint: disable=invalid-name
        len_dataset = get_len(dataset)
        if y is not None:
            len_y = get_len(y)
            if len_dataset != len_y:
                raise ValueError("Cannot perform a CV split if dataset and y "
                                 "have different lengths.")

        args = (np.arange(len_dataset),)
        if self._is_stratified(cv):
            args = args + (to_numpy(y),)

        idx_train, idx_valid = next(iter(cv.split(*args, groups=groups)))
        dataset_train = torch.utils.data.dataset.Subset(dataset, idx_train)
        dataset_valid = torch.utils.data.dataset.Subset(dataset, idx_valid)
        return dataset_train, dataset_valid

    def __repr__(self):
        # pylint: disable=useless-super-delegation
        return super(CVSplit, self).__repr__()
