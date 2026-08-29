from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.decomposition import PCA
from sklearn.preprocessing import LabelEncoder


class EncodedClassifier(ClassifierMixin, BaseEstimator):  # type: ignore[misc]
    """Adapt numeric-label-only classifiers to the project's string label contract."""

    def __init__(self, estimator: Any) -> None:
        self.estimator = estimator

    def fit(self, values: Any, labels: Any) -> EncodedClassifier:
        self.label_encoder_ = LabelEncoder().fit(labels)
        self.classes_ = np.asarray(self.label_encoder_.classes_, dtype=np.str_)
        self.estimator_ = clone(self.estimator)
        self.estimator_.fit(values, self.label_encoder_.transform(labels))
        return self

    def predict(self, values: Any) -> np.ndarray[Any, np.dtype[np.str_]]:
        encoded = np.asarray(self.estimator_.predict(values), dtype=np.int64)
        return np.asarray(self.label_encoder_.inverse_transform(encoded), dtype=np.str_)

    def predict_proba(self, values: Any) -> np.ndarray[Any, np.dtype[np.float64]]:
        return np.asarray(self.estimator_.predict_proba(values), dtype=np.float64)


class PCAReconstructionDetector(BaseEstimator):  # type: ignore[misc]
    """Score distance from a training-fitted linear subspace as reconstruction error."""

    def __init__(self, explained_variance: float = 0.90, random_state: int = 42) -> None:
        self.explained_variance = explained_variance
        self.random_state = random_state

    def fit(self, values: Any, labels: Any = None) -> PCAReconstructionDetector:
        del labels
        matrix = np.asarray(values, dtype=np.float64)
        self.pca_ = PCA(
            n_components=self.explained_variance,
            svd_solver="full",
            random_state=self.random_state,
        ).fit(matrix)
        return self

    def score_samples(self, values: Any) -> np.ndarray[Any, np.dtype[np.float64]]:
        matrix = np.asarray(values, dtype=np.float64)
        reconstructed = self.pca_.inverse_transform(self.pca_.transform(matrix))
        # sklearn anomaly estimators define larger native scores as more normal.
        return -np.mean(np.square(matrix - reconstructed), axis=1)
