# BUILD_STATUS.md — BotYo V1

Date : 2026-04-03 09:56 UTC

## Résultats des tests

```text
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.0.2, pluggy-1.6.0 -- C:\Users\Braul\AppData\Local\Programs\Python\Python312\python.exe
cachedir: .pytest_cache
rootdir: D:\Yoann\Documents\PROJET LOGICIEL\BotYo
plugins: anyio-4.13.0
collecting ... collected 38 items

tests/test_alerts.py::test_format_message_contains_all_required_fields PASSED [  2%]
tests/test_alerts.py::test_cooldown_blocks_second_identical_alert PASSED [  5%]
tests/test_alerts.py::test_max_three_active_alerts_blocks_fourth PASSED  [  7%]
tests/test_alerts.py::test_reject_if_rr_below_minimum PASSED             [ 10%]
tests/test_alerts.py::test_reject_if_stop_missing PASSED                 [ 13%]
tests/test_alerts.py::test_shadow_mode_is_not_sent_to_telegram PASSED    [ 15%]
tests/test_api.py::test_get_root_returns_200 PASSED                      [ 18%]
tests/test_api.py::test_get_admin_returns_200 PASSED                     [ 21%]
tests/test_api.py::test_get_journal_returns_200 PASSED                   [ 23%]
tests/test_api.py::test_get_api_status_returns_valid_json PASSED         [ 26%]
tests/test_api.py::test_get_api_alerts_active_returns_json_list PASSED   [ 28%]
tests/test_api.py::test_post_admin_config_with_valid_parameter PASSED    [ 31%]
tests/test_db.py::test_insert_candle PASSED                              [ 34%]
tests/test_db.py::test_get_recent_candles_returns_latest_in_order PASSED [ 36%]
tests/test_db.py::test_candle_uniqueness_constraint PASSED               [ 39%]
tests/test_indicators.py::test_ema_matches_manual_series PASSED          [ 42%]
tests/test_indicators.py::test_atr_on_three_candles PASSED               [ 44%]
tests/test_indicators.py::test_rsi_flat_series_is_neutral PASSED         [ 47%]
tests/test_indicators.py::test_adx_low_on_range_and_high_on_trend PASSED [ 50%]
tests/test_indicators.py::test_detect_swings_and_volume_ma PASSED        [ 52%]
tests/test_probability.py::test_shadow_mode_if_insufficient_samples PASSED [ 55%]
tests/test_probability.py::test_probability_between_zero_and_one PASSED  [ 57%]
tests/test_probability.py::test_recalibration_on_synthetic_history PASSED [ 60%]
tests/test_regime.py::test_bull_trend_on_aligned_indicators PASSED       [ 63%]
tests/test_regime.py::test_bear_trend_on_inverted_indicators PASSED      [ 65%]
tests/test_regime.py::test_range_on_low_adx PASSED                       [ 68%]
tests/test_regime.py::test_high_volatility_noise_on_high_atr_ratio PASSED [ 71%]
tests/test_regime.py::test_low_quality_market_on_weak_volume PASSED      [ 73%]
tests/test_scoring.py::test_score_total_does_not_exceed_100 PASSED       [ 76%]
tests/test_scoring.py::test_weights_sum_to_100 PASSED                    [ 78%]
tests/test_scoring.py::test_reject_on_insufficient_rr PASSED             [ 81%]
tests/test_scoring.py::test_reject_on_score_below_65 PASSED              [ 84%]
tests/test_scoring.py::test_alert_priority_on_score_ge_83 PASSED         [ 86%]
tests/test_setups.py::test_trend_continuation_long_detected PASSED       [ 89%]
tests/test_setups.py::test_breakout_detected_on_compression_and_volume PASSED [ 92%]
tests/test_setups.py::test_reversal_detected_on_major_level_and_break_structure PASSED [ 94%]
tests/test_setups.py::test_range_rotation_detected_only_in_range PASSED  [ 97%]
tests/test_setups.py::test_incompatible_regime_is_rejected PASSED        [100%]

============================= 38 passed in 2.12s ==============================
```

## Checklist Phase 7

### Données

- [x] SQLite WAL actif (`PRAGMA journal_mode = wal`)
- [x] Historique REST bootstrappé pour BTCUSDT, ETHUSDT, XRPUSDT sur `1D`, `4H`, `1H`
- [x] Historique disponible en base : `1D=720`, `4H=721`, `1H=721` par actif
- [x] WebSocket Kraken v2 validé sur les 9 couples symbole/timeframe : `BTCUSDT|ETHUSDT|XRPUSDT` x `1D|4H|1H`
- [x] Bougies non clôturées exclues des décisions : REST ignore la dernière bougie, WebSocket filtre les timestamps de clôture futurs

### Indicateurs

- [x] EMA 20/50/200 incrémentales validées
- [x] ATR 14 validé
- [x] RSI 14 validé
- [x] ADX 14 validé
- [x] Swing highs / lows et moyenne volume validés

### Régime et Setups

- [x] Classification `bull_trend`, `bear_trend`, `range`, `high_volatility_noise`, `low_quality_market` validée par tests
- [x] Régimes interdits bloquent les alertes
- [x] Les 4 setups sont détectés sur jeux synthétiques favorables
- [x] Régime incompatible rejeté automatiquement
- [x] Aucun setup basé sur bougie non clôturée

### Scoring et Probabilité

- [x] Score borné `0..100`, pondérations = `100`
- [x] Seuils `reject / shadow / alert / alert_priority` appliqués
- [x] Shadow mode actif si échantillons insuffisants
- [x] Calibration isotonic disponible et validée sur données synthétiques
- [x] Après backtest actuel : `49` échantillons, donc `mode=shadow`, `calibrated=False`

### Alertes

- [x] Format Telegram conforme aux champs DEVBOOK
- [x] Cooldowns et limites simultanées validés
- [x] Filtres qualité actifs : stop, R/R, probabilité, wick, late entry
- [x] En `shadow_live`, aucun envoi Telegram réel

### Web

- [x] `python app/main.py` démarre proprement
- [x] `/health` retourne `200`
- [x] `/`, `/admin`, `/journal` et APIs principales retournent `200`
- [x] Rechargement de config sans redémarrage validé par tests

### Modes

- [x] Démarrage par défaut en `shadow_live`
- [x] Backtest validé : `49` signaux stockés en base avec `mode=backtest`
- [x] Aucun envoi Telegram en `shadow_live`
- [x] `live_alert` ne peut pas être activé automatiquement par le bot ; activation prévue via l'Admin uniquement

## Notes de conformité

- Les endpoints Kraken OHLC utilisés en V1 sont publics. Les clés Kraken fournies n'ont pas été écrites dans le code ni dans `config/bot.yaml`, conformément aux règles du repo sur les secrets.
- `config/bot.yaml` versionné conserve `telegram_bot_token` et `telegram_chat_id` vides. L'envoi Telegram réel en `live_alert` n'a donc pas été exécuté dans cette validation finale.
- Le bot est prêt pour un démarrage en `shadow_live`. Le passage en `live_alert` reste conditionné à une action manuelle via `/admin` et aux seuils de calibration DEVBOOK.

## Statut final : VALIDÉ

Le build BotYo V1 est conforme aux spécifications implémentées et validées dans ce workspace.
