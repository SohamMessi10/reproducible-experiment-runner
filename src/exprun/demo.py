"""The demo: handwritten digits, classified with scikit-learn, as an experiment exprun can run.

`exprun demo FOLDER` writes a ready-to-run folder:

    data/digits.csv      1,797 8x8 images of digits as 64 pixel values (0-16) and a label
    data/SOURCE.txt      where the data comes from, and its licence
    logistic.toml        logistic regression
    forest.toml          a random forest

The data ships inside scikit-learn, so the demo works offline. It is a copy of the test set of the
UCI "Optical Recognition of Handwritten Digits" dataset (Alpaydin and Kaynak, 1998), licensed CC BY
4.0. The scikit-learn and NumPy it needs are the optional `demo` extra:
`pip install "reproducible-experiment-runner[demo]"`.

`train` is the task. It splits the data with the run's seed, fits the model named in the params,
and returns accuracy, macro F1 and log loss on the held-out part. It writes `probabilities.csv`, every
predicted probability at full precision (17 significant digits), so a change in the last bit of any
number makes the output file differ. That is what a rerun's byte comparison checks.
"""

from __future__ import annotations

from pathlib import Path

SOURCE = """\
Handwritten digits: 1,797 images of 8x8 pixels (values 0-16), with labels 0-9.

This is the copy of the test set of the UCI "Optical Recognition of Handwritten Digits" dataset that
ships with scikit-learn (sklearn.datasets.load_digits), written out as CSV by `exprun demo`.

Alpaydin, E. & Kaynak, C. (1998). Optical Recognition of Handwritten Digits [Dataset].
UCI Machine Learning Repository. https://doi.org/10.24432/C50P49
Licensed under Creative Commons Attribution 4.0 International (CC BY 4.0).
"""

LOGISTIC = """\
[experiment]
name = "digits-logistic"
task = "exprun.demo:train"
seed = 42
description = "logistic regression on handwritten digits"

[inputs]
digits = "data/digits.csv"

[params]
test_size = 0.25
model = "logistic"
logistic = { C = 1.0, max_iter = 5000 }
"""

FOREST = """\
[experiment]
name = "digits-forest"
task = "exprun.demo:train"
seed = 42
description = "random forest on handwritten digits"

[inputs]
digits = "data/digits.csv"

[params]
test_size = 0.25
model = "forest"
forest = { n_estimators = 200, max_depth = 12, n_jobs = 1 }
"""

FILES = {"data/digits.csv": None, "data/SOURCE.txt": SOURCE, "logistic.toml": LOGISTIC, "forest.toml": FOREST}


def export(folder: Path) -> list[Path]:
    """Write the demo folder. Refuses to overwrite any of its files."""
    from sklearn.datasets import load_digits

    folder = Path(folder)
    existing = [folder / name for name in FILES if (folder / name).exists()]
    if existing:
        raise FileExistsError(f"{existing[0]} already exists; choose an empty folder")
    (folder / "data").mkdir(parents=True, exist_ok=True)
    images, labels = load_digits(return_X_y=True)
    header = ",".join([f"pixel_{i}" for i in range(images.shape[1])] + ["label"])
    rows = [",".join(str(int(v)) for v in image) + f",{int(label)}" for image, label in zip(images, labels, strict=True)]
    (folder / "data" / "digits.csv").write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    for name, text in FILES.items():
        if text is not None:
            (folder / name).write_text(text, encoding="utf-8")
    return [folder / name for name in FILES]


def train(ctx):
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, f1_score, log_loss
    from sklearn.model_selection import train_test_split

    data = np.loadtxt(ctx.inputs["digits"], delimiter=",", skiprows=1)
    features, labels = data[:, :-1] / 16.0, data[:, -1].astype(int)
    train_x, test_x, train_y, test_y = train_test_split(features, labels, test_size=ctx.params["test_size"],
                                                        random_state=ctx.seed, stratify=labels)
    kind = ctx.params["model"]
    settings = dict(ctx.params.get(kind, {}))
    if kind == "logistic":
        model = LogisticRegression(random_state=ctx.seed, **settings)
    elif kind == "forest":
        model = RandomForestClassifier(random_state=ctx.seed, **settings)
    else:
        raise ValueError(f"params.model must be \"logistic\" or \"forest\", not {kind!r}")
    model.fit(train_x, train_y)
    probabilities = model.predict_proba(test_x)
    predicted = model.classes_[probabilities.argmax(axis=1)]
    np.savetxt(ctx.output_dir / "probabilities.csv", probabilities, fmt="%.17g", delimiter=",")
    metrics = {
        "accuracy": float(accuracy_score(test_y, predicted)),
        "macro_f1": float(f1_score(test_y, predicted, average="macro")),
        "log_loss": float(log_loss(test_y, probabilities, labels=model.classes_)),
        "n_train": len(train_y),
        "n_test": len(test_y),
    }
    if kind == "logistic":
        metrics["iterations"] = int(np.max(model.n_iter_))
        metrics["converged"] = bool(np.max(model.n_iter_) < settings.get("max_iter", 100))
    print(f"{kind}: accuracy {metrics['accuracy']:.4f} on {metrics['n_test']} held-out digits")
    return metrics
