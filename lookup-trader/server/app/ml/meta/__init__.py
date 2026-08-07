"""Binary take/skip meta-model over sparse trade events.

Separate from `app.ml.outcome` rather than an extension of it. That package is
built around a three-class `CLASS_ORDER` and a 137-column `INPUT_FEATURES`, both
module constants baked into function bodies, and two live consumers validate
against them: `outcome/infer.py` checks an artifact's `class_order` and
`input_features`, and `services/data_health.py` reads `multiclass_log_loss` out
of its metrics. Widening those constants to fit a binary model would break both.
"""
