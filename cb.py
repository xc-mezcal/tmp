import optuna
import numpy as np
import pandas as pd
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import average_precision_score, make_scorer

optuna.logging.set_verbosity(optuna.logging.WARNING)

# ── Load your data ──────────────────────────────────────────────
# X_train: (1906, 99), y_train: (1906,)
# X_test:  (477,  99), y_test:  (477,)

# ── Optional but recommended: feature pre-selection ─────────────
# Uncomment this block to trim features before tuning
"""
from xgboost import XGBClassifier as XGB
pre_model = XGB(
    n_estimators=300, max_depth=5, learning_rate=0.05,
    scale_pos_weight=len(y_train[y_train==0]) / len(y_train[y_train==1]),
    random_state=42, eval_metric='aucpr'
)
pre_model.fit(X_train, y_train)

importances = pd.Series(pre_model.feature_importances_, index=X_train.columns)
top_features = importances.nlargest(40).index.tolist()
print(f"Keeping {len(top_features)} features out of {X_train.shape[1]}")

X_train = X_train[top_features]
X_test = X_test[top_features]
"""

# ── Class imbalance ratio ──────────────────────────────────────
neg_count = (y_train == 0).sum()
pos_count = (y_train == 1).sum()
imbalance_ratio = neg_count / pos_count  # ~1.90

scorer = make_scorer(average_precision_score, needs_proba=True)
cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)


def objective(trial):
    params = {
        'max_depth':         trial.suggest_int('max_depth', 2, 8),
        'learning_rate':     trial.suggest_float('learning_rate', 0.01, 0.3, log=True),
        'n_estimators':      trial.suggest_int('n_estimators', 100, 800, step=50),
        'subsample':         trial.suggest_float('subsample', 0.5, 0.9),
        'colsample_bytree':  trial.suggest_float('colsample_bytree', 0.3, 0.8),
        'gamma':             trial.suggest_float('gamma', 0.0, 5.0),
        'min_child_weight':  trial.suggest_int('min_child_weight', 1, 20),
        'scale_pos_weight':  trial.suggest_float('scale_pos_weight', 1.0, imbalance_ratio * 1.5),
        'reg_alpha':         trial.suggest_float('reg_alpha', 1e-4, 10.0, log=True),
        'reg_lambda':        trial.suggest_float('reg_lambda', 1e-4, 10.0, log=True),
        # fixed
        'eval_metric':       'aucpr',
        'random_state':      42,
        'use_label_encoder':  False,
    }

    model = XGBClassifier(**params)

    # CV score (what we optimize)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring=scorer)

    # Train score (to monitor overfitting — NOT used for optimization)
    model.fit(X_train, y_train)
    train_score = average_precision_score(y_train, model.predict_proba(X_train)[:, 1])

    # Store train score for later comparison
    trial.set_user_attr('train_ap', train_score)
    trial.set_user_attr('cv_std', cv_scores.std())

    return cv_scores.mean()


# ── Run optimization ───────────────────────────────────────────
study = optuna.create_study(direction='maximize')
study.optimize(objective, n_trials=150, show_progress_bar=True)

# ── Results ────────────────────────────────────────────────────
print(f"\nBest CV Average Precision: {study.best_value:.4f}")
print(f"Best params: {study.best_params}")

# ── Overfitting diagnostic ─────────────────────────────────────
results = study.trials_dataframe()
results['train_ap'] = [t.user_attrs.get('train_ap', np.nan) for t in study.trials]
results['cv_std']   = [t.user_attrs.get('cv_std', np.nan) for t in study.trials]
results['overfit_gap'] = results['train_ap'] - results['value']  # value = cv mean

print("\n── Overfitting Check (top 10 trials by CV score) ──")
top10 = results.nlargest(10, 'value')[['number', 'value', 'train_ap', 'overfit_gap', 'cv_std']]
top10.columns = ['trial', 'cv_ap', 'train_ap', 'gap', 'cv_std']
print(top10.to_string(index=False))

# Flag: if best trial has gap > 0.15, you're likely overfitting
best_gap = results.loc[results['value'].idxmax(), 'overfit_gap']
if best_gap > 0.15:
    print(f"\n⚠️  Train-CV gap is {best_gap:.3f} — likely overfitting.")
    print("    → Try enabling feature pre-selection above")
    print("    → Or tighten max_depth range to (2, 5)")

# ── Final evaluation on held-out test ──────────────────────────
best_model = XGBClassifier(**study.best_params, eval_metric='aucpr',
                            random_state=42, use_label_encoder=False)
best_model.fit(X_train, y_train)
test_ap = average_precision_score(y_test, best_model.predict_proba(X_test)[:, 1])
print(f"\nTest Average Precision: {test_ap:.4f}")
print(f"CV → Test gap: {abs(study.best_value - test_ap):.4f}")
