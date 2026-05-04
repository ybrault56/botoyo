# DEVBOOK.md — BotYo V1

> Spécification technique complète.
> Ce fichier est la source de vérité pour toute décision d'implémentation.
> L'agent Codex doit le consulter avant et pendant chaque phase de développement.

---

## 1. Vue d'ensemble

BotYo est un bot d'alerte crypto personnel qui surveille BTC, ETH et XRP en continu, analyse des signaux techniques multi-timeframes, surveille des catalyseurs externes `whales`, et envoie une alerte uniquement quand :

- le setup technique est valide
- la probabilité calibrée dépasse le seuil minimal
- le ratio rendement/risque est acceptable
- l'horizon de trade correspond au style swing (pas de scalping, pas d'intraday pur)

**Le bot n'exécute pas les ordres en V1.**  
Il détecte, score, filtre, puis alerte avec un délai d'action clair.

### 1.1 Profil analytique de référence

Le profil V1 retenu pour le swing court est :

- horizon opératoire : `1 à 7 jours`
- cascade multi-timeframe : `4H` pour la direction et la structure, `1H` pour la confirmation, `15M` pour l'entrée
- priorité absolue à la structure de marché (`HH/HL` ou `LH/LL`) avant tout indicateur
- confluence obligatoire entre structure, niveau, momentum et volume
- qualité prioritaire sur fréquence : peu de signaux, mais des signaux propres

---

## 2. Périmètre fonctionnel V1

### Ce que BotYo fait

- Surveiller BTC, ETH, XRP (paires USDT)
- Analyser 3 unités de temps cohérentes (`4H`, `1H`, `15M`)
- Générer un score de probabilité exploitable
- Envoyer uniquement les alertes de haute qualité
- Indiquer le sens, le niveau d'entrée, l'invalidation, les objectifs, et le temps maximum pour agir
- Journaliser tous les signaux pour mesurer la vraie performance
- Exposer un dashboard web local avec configuration admin
- Surveiller les posts X des comptes listés dans `whales/X.md`
- Surveiller les wallets BTC / ETH / XRP listés dans `whales/WW.md`
- Soumettre les posts X à `GPT-5.4` avant toute alerte sociale
- Détecter un biais whales `24h` clair par actif et émettre une méta-alerte `Whale trend 24h` sur Telegram

### Ce que BotYo ne fait pas

- Prendre des positions automatiquement
- Surtrader ou envoyer des alertes à faible conviction
- Utiliser un flux social, news ou order flow généraliste non ciblé
- Surveiller des comptes X ou wallets non listés dans les documents `whales`
- Prétendre prédire le marché
- Fonctionner avec des signaux basés sur une bougie non clôturée

---

## 3. Stack technique

### Runtime

| Composant | Choix | Justification |
|---|---|---|
| Langage | Python 3.12 | stable, async mature, écosystème complet |
| Paradigme | asyncio | WebSocket propre, faible overhead CPU |
| Source live | Kraken WebSocket v2 | fiable, gratuit, flux OHLC natif |
| Source bootstrap | Kraken REST `/public/OHLC` | resync ponctuel uniquement |
| Calcul | NumPy incrémental | zéro recalcul complet, faible RAM |
| Stockage | SQLite WAL | zéro infra, ultra rapide local, portable |
| Backend | FastAPI + Uvicorn | async natif, performant |
| Templates | Jinja2 | pas de SPA lourd |
| Interactivité | HTMX | UI fluide sans Node |
| Graphiques | Chart.js | léger, suffit pour métriques |
| Alertes | Telegram Bot API (HTTP direct) | simple, pas de SDK lourd |
| X social | X API | polling des comptes listés dans `whales/X.md` |
| BTC whales | Blockstream Esplora API | gratuit, sans clé, simple pour suivi wallet |
| ETH whales | Alchemy WebSocket | filtre pending tx sur adresses suivies |
| XRP whales | XRPL WebSocket | souscription native sur comptes suivis |
| LLM social | OpenAI Responses API `gpt-5.4` | filtrage de pertinence avant alerte X |
| Sérialisation | orjson | ultra rapide, faible overhead |
| Config | YAML + pyyaml | lisible, éditable depuis l'UI Admin |

### Dépendances (requirements.txt)

```
fastapi>=0.111.0
uvicorn>=0.30.0
aiohttp>=3.9.0
httpx>=0.27.0
orjson>=3.10.0
numpy>=1.26.0
pyyaml>=6.0.1
jinja2>=3.1.0
websockets>=12.0
```

### Ce qui est exclu

React, Next.js, Docker, Redis, PostgreSQL, pandas (dans la boucle chaude), microservices, ML lourd, GPU, frameworks UI desktop.

---

## 4. Architecture des données

### 4.1 Source de données

```
Kraken WebSocket v2
  canal: ohlc
  symboles: BTC/USDT, ETH/USDT, XRP/USDT
  timeframes: 15, 60, 240 (minutes)

Kraken REST /public/OHLC
  usage: bootstrap initial + resync après coupure
  limite: 720 bougies par appel
  note: dernière bougie = période en cours, à ignorer pour décision

X API
  usage: polling des timelines des comptes listés dans whales/X.md
  auth: bearer token depuis .env

OpenAI Responses API
  model: gpt-5.4
  usage: décider si un post X est pertinent ou non
  auth: OPENAI_API_KEY depuis .env

Blockstream Esplora API
  usage: surveillance des wallets BTC listés dans whales/WW.md
  auth: aucune

Alchemy WebSocket
  usage: surveillance des wallets ETH listés dans whales/WW.md
  auth: API key depuis .env

XRPL WebSocket public
  usage: surveillance des wallets XRP listés dans whales/WW.md
  auth: aucune
```

### 4.2 Règles de qualité des données

- Utiliser **uniquement les bougies clôturées** pour toute décision
- Si une bougie manque sur un timeframe : suspendre l'analyse de l'actif
- Si volume anormalement faible (< 70% moyenne 20 bougies) : réduire le score
- Si variation anormale non confirmée par la structure : ne pas alerter
- Ne jamais décider sur la bougie courante non clôturée
- Au démarrage, resynchroniser chaque symbole/timeframe via REST avant d'ouvrir le flux WebSocket live
- Si la base contient déjà des bougies, reprendre depuis la dernière bougie stockée avec un recouvrement minimal de `2` intervalles
- Pendant l'exécution, relancer une resynchronisation REST périodique, `60` secondes par défaut, pour rattraper les bougies clôturées si le flux live prend du retard
- Si un flux OHLC WebSocket reste silencieux au-delà de son intervalle attendu plus une petite marge, fermer puis rouvrir la connexion
- Après cette resynchronisation de lancement, rejouer les dernières bougies `15M` pour backfiller les signaux manqués pendant l'arrêt
- Puis recalculer immédiatement les régimes, le lifecycle des signaux et les alertes courantes
- Les alertes `whales` doivent être dédupliquées par `event_id`
- Un post X ne peut déclencher une alerte que si la réponse LLM n'est pas `PAS PERTINENT`
- Un mouvement wallet ne peut déclencher une alerte que si le montant USD dépasse le seuil strict configuré
- Une méta-alerte `wallet_trend` ne peut être émise que si la fenêtre glissante `24h` contient au moins `2` mouvements, un net USD absolu au moins égal au seuil strict, et une dominance du biais d'au moins `60%`

### 4.3 Historique minimal requis

- Ne jamais supprimer un historique SQLite propre deja collecte.
- Si Kraken REST intraday ne permet pas, sur une base vide, de reconstruire seul `720 jours`, BotYo doit preserver l'archive locale, exposer la couverture reelle par `symbol/timeframe`, et marquer explicitement la couverture partielle.

```yaml
historical_lookback_days: 720
warmup_bars:
  "4H": 500
  "1H": 720
  "15M": 720
```

### 4.4 Schéma SQLite

#### Table `candles`

```sql
CREATE TABLE IF NOT EXISTS candles (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol      TEXT NOT NULL,
  timeframe   TEXT NOT NULL,
  open_time   INTEGER NOT NULL,
  open        REAL NOT NULL,
  high        REAL NOT NULL,
  low         REAL NOT NULL,
  close       REAL NOT NULL,
  volume      REAL NOT NULL,
  closed      INTEGER NOT NULL DEFAULT 1,
  created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
  UNIQUE(symbol, timeframe, open_time)
);
```

#### Table `signals`

```sql
CREATE TABLE IF NOT EXISTS signals (
  id                  TEXT PRIMARY KEY,
  symbol              TEXT NOT NULL,
  direction           TEXT NOT NULL,
  setup_type          TEXT NOT NULL,
  regime              TEXT NOT NULL,
  score               REAL NOT NULL,
  probability         REAL,
  entry_low           REAL NOT NULL,
  entry_high          REAL NOT NULL,
  stop                REAL NOT NULL,
  target1             REAL NOT NULL,
  target2             REAL NOT NULL,
  rr                  REAL NOT NULL,
  validity_hours      REAL NOT NULL,
  invalidation_rule   TEXT NOT NULL,
  features_json       TEXT,
  emitted_at          INTEGER NOT NULL,
  expires_at          INTEGER NOT NULL,
  status              TEXT NOT NULL DEFAULT 'active',
  result_r            REAL,
  closed_at           INTEGER,
  mode                TEXT NOT NULL,
  comment             TEXT
);
```

#### Table `metrics`

```sql
CREATE TABLE IF NOT EXISTS metrics (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  computed_at INTEGER NOT NULL,
  scope       TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       REAL NOT NULL
);
```

#### Table `external_alerts`

```sql
CREATE TABLE IF NOT EXISTS external_alerts (
  id              TEXT PRIMARY KEY,
  source          TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  signal          TEXT NOT NULL,
  probability     REAL NOT NULL,
  observed_at     INTEGER NOT NULL,
  title           TEXT,
  message         TEXT NOT NULL,
  metadata_json   TEXT,
  delivery_status TEXT NOT NULL DEFAULT 'detected',
  created_at      INTEGER NOT NULL DEFAULT (unixepoch())
);
```

#### Table `indicator_states`

```sql
CREATE TABLE IF NOT EXISTS indicator_states (
  symbol         TEXT NOT NULL,
  timeframe      TEXT NOT NULL,
  last_open_time INTEGER NOT NULL,
  state_json     TEXT NOT NULL,
  snapshot_json  TEXT NOT NULL,
  updated_at     INTEGER NOT NULL DEFAULT (unixepoch()),
  PRIMARY KEY(symbol, timeframe)
);
```

#### Activation WAL

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
```

---

## 5. Indicateurs techniques

Le runtime live s'appuie sur un etat incremental persiste en SQLite (`indicator_states`) pour les timeframes actifs. Les replays de lancement et les resynchronisations avancent cet etat par bougie au lieu de rechauffer toute la fenetre depuis zero a chaque analyse.

Tous les indicateurs sont calculés de façon **incrémentale** avec NumPy. Pas de recalcul complet de la série à chaque bougie.

### 5.0 Lecture prioritaire : structure de marché

- La structure prime sur les indicateurs.
- Une structure `HH/HL` valide favorise les longs.
- Une structure `LH/LL` valide favorise les shorts.
- Un trade contre la structure dominante `4H` est interdit sauf dans les setups explicitement autorisés (`reversal`, `range_rotation`).
- La structure est lue en priorité sur `4H`, confirmée sur `1H`, puis exécutée sur `15M`.
- La lecture de structure utilise une fenêtre de swings configurable ; la valeur opératoire V1 par défaut est `3`

### 5.1 EMA (Exponential Moving Average)

- Périodes : 20, 50, 200
- Timeframes : `4H` pour la tendance de fond, `1H` pour la structure et le retracement, `15M` pour l'entrée
- Formule incrémentale : `EMA_t = close_t * k + EMA_(t-1) * (1 - k)` avec `k = 2 / (period + 1)`
- État persisté : valeur EMA précédente
- Usages clés :
  - `EMA20` : support / résistance dynamique en swing court
  - croisement `EMA20/50` sur `4H` : changement de tempo ou de direction
  - prix au-dessus de `EMA200` : contexte structurel haussier, en dessous : contexte structurel baissier

### 5.2 ATR (Average True Range)

- Période : 14
- True Range : `max(high - low, abs(high - prev_close), abs(low - prev_close))`
- Moyenne : RMA (Wilder's smoothing) : `ATR_t = (ATR_(t-1) * 13 + TR_t) / 14`
- Usage : calcul des distances stop/entrée, détection volatilité anormale

### 5.3 RSI (Relative Strength Index)

- Période : 14
- Smoothing Wilder
- Usage : filtre de confirmation, jamais indicateur isolé
- Survendu < `30`, suracheté > `70` (seuils opératoires)
- Une divergence RSI valide apporte un bonus de confiance important, surtout sur `BTC` et `ETH`

### 5.4 ADX (Average Directional Index)

- Période : 14
- Composants : +DI, -DI, ADX
- Smoothing Wilder
- Usage : classification régime (`ADX >= 20 = tendance`, `ADX < 18 = range`)

### 5.5 Swing Highs / Swing Lows

- Fenêtre : 5 bougies (configurable)
- Un swing high est un pivot dont le high est supérieur aux N bougies voisines gauche et droite
- Un swing low est un pivot dont le low est inférieur aux N bougies voisines gauche et droite
- Usage : détection structure HH/HL/LH/LL, niveaux de stop

### 5.6 Volume MA

- Période : 20 bougies
- Usage : détection volume breakout, filtre qualité

### 5.7 MACD

- Paramètres : `12-26-9`
- Usage : confirmation d'élan et lecture d'essoufflement
- Un croisement haussier ou baissier sur `1H` ou `15M` sert de confirmation de timing
- Un histogramme qui se contracte signale une perte de momentum et peut préparer un reversal

### 5.8 Niveaux techniques et Fibonacci

- Retracements suivis : `0.382`, `0.5`, `0.618`
- Usage principal : repérer les zones de retracement propres en `trend_continuation`
- Les niveaux ronds (`BTC 50k / 60k / 100k`, `ETH` par centaines, `XRP` par `0.10`) sont considérés comme zones de liquidité et de réaction

### 5.9 Spécificités par actif

- `BTCUSDT` : actif le plus propre techniquement, compatible avec Fibonacci et niveaux ronds
- `ETHUSDT` : suit souvent `BTC`, mais peut surperformer ou sous-performer selon le cycle ; le ratio `ETH/BTC` est une information utile mais non bloquante en V1
- `XRPUSDT` : actif plus sale et plus violent ; confirmation volume plus stricte et préférence pour des supports / résistances très nets

---

## 6. Moteur de régime de marché

La classification du régime est **obligatoire** avant toute détection de setup.

### 6.1 Régimes possibles

| Régime | Alertes autorisées |
|---|---|
| `bull_trend` | oui |
| `bear_trend` | oui |
| `range` | oui (setups range uniquement) |
| `high_volatility_noise` | non |
| `low_quality_market` | non |

### 6.2 Règles de classification

**bull_trend** (toutes conditions requises sur `4H`) :
- EMA50 > EMA200
- Clôture > EMA50
- ADX >= 20
- Structure `4H` : séquence `HH/HL` confirmée
- Structure `1H` non contradictoire

**bear_trend** (toutes conditions requises sur `4H`) :
- EMA50 < EMA200
- Clôture < EMA50
- ADX >= 20
- Structure `4H` : séquence `LH/LL` confirmée
- Structure `1H` non contradictoire

**range** (toutes conditions requises) :
- ADX `4H` < 20
- Structure `4H` neutre ou range
- Une rotation `1H` transitoire n'invalide pas le range si le `4H` reste non directionnel

**high_volatility_noise** :
- ATR `1H` / prix > `atr_price_ratio_max` (défaut : 0.06)
- Mèches anormales répétées (mèche > 60% de la bougie)
- Structure contradictoire entre `4H` et `1H`

**low_quality_market** :
- Faiblesse de volume persistante : volume courant et volume moyen recent < 70% de la moyenne 20 bougies
- Compression peu exploitable (range < 0.5 ATR)
- Absence de niveau propre pour stop

### 6.3 Règle opérationnelle

```python
REGIMES_AUTORISES_ALERTE = {"bull_trend", "bear_trend", "range"}
REGIMES_INTERDITS_ALERTE = {"high_volatility_noise", "low_quality_market"}
```

---

## 7. Setups autorisés en V1

### Règle commune à tous les setups

Un setup ne doit jamais être basé sur un seul indicateur. Il doit toujours combiner : contexte régime + structure + timing + invalidation + R/R.

La logique d'entrée type est :

1. lire la tendance et la structure sur `4H`
2. attendre un retracement ou une zone technique sur `1H`
3. confirmer sur `1H` ou `15M` via RSI / MACD / structure / bougie de retournement
4. entrer sur `15M` avec un stop sous ou au-dessus du dernier pivot significatif
5. refuser tout setup dont le `R/R` n'atteint pas `1:2`

---

### Setup 1 : Trend Continuation

**Long**
- Régime : `bull_trend`
- Tendance `4H` haussière confirmée (`EMA20 > EMA50 > EMA200` et structure `HH/HL`)
- Pullback `1H` vers `EMA20` ou `EMA50`
- Confluence recherchée : zone technique, Fibonacci `0.5 / 0.618`, niveau rond, support clair
- Confirmation `1H` ou `15M` par bougie de renversement, RSI qui repart, croisement MACD ou break local de structure
- Execution de reference V1 : `market_on_close` sur la bougie `15M` de confirmation
- Stop technique : sous le swing low significatif de `15M` ou `1H`
- R/R minimal respecté

**Short** : conditions inverses (régime `bear_trend`)

**Validité :** 12h

---

### Setup 2 : Breakout propre

- Régime : `bull_trend`, `bear_trend` ou `range`
- Compression préalable sur `1H` (`>= 6` bougies en range étroit)
- Cassure confirmée par clôture (pas de mèche seule)
- Volume breakout > `2.0x` la moyenne des 20 dernières bougies, plus strict sur `XRP`
- Espace libre suffisant jusqu'à la prochaine résistance/support (>= 2 ATR)
- Pas de cassure directement dans une zone majeure opposée
- Confirmation `15M` ou retest acceptable
- Execution de reference V1 : `market_on_close` sur la bougie `15M` de confirmation
- Stop : sous le range de compression (long) / au-dessus (short)

**Validité :** 18h

---

### Setup 3 : Reversal structuré

- Régime : `bull_trend`, `bear_trend` ou `range`
- Arrivée sur niveau technique fort `4H` ou `1H` (swing majeur, zone de structure)
- Excès de mouvement (extension >= 1.5 ATR au-delà du niveau)
- Break de structure sur `15M` ou `1H` dans le sens du retournement
- Stop technique clair au-delà de l'excès, ancré sur les dernières bougies de retournement `15M`
- Execution de reference V1 : `market_on_close` sur la bougie `15M` de confirmation
- R/R suffisant (>= 2.0)
- Divergence momentum secondaire confirmative (optionnel mais bonus de score)

**Validité :** 16h

---

### Setup 4 : Range Rotation

- Régime : `range` uniquement
- Prix proche d'une borne identifiable sur `1H` (< 0.3 ATR de la borne)
- Rejet confirmé par clôture à l'intérieur du range
- Invalidation propre : clôture hors borne
- Objectif minimum : milieu du range
- Objectif idéal : borne opposée
- Confirmation `15M` via rejet clair et RSI qui repart
- Execution de reference V1 : `market_on_close` sur la bougie `15M` de confirmation

**Validité :** 16h

---

## 8. Moteur de scoring

### 8.1 Grille de pondération (total = 100)

| Critère | Poids |
|---|---|
| Régime de marché | 20 |
| Structure multi-timeframe | 20 |
| Qualité du setup | 15 |
| Localisation technique | 10 |
| Momentum | 10 |
| Volume | 10 |
| Qualité d'entrée | 5 |
| Clarté du stop | 5 |
| Ratio R/R | 5 |

### 8.2 Seuils de décision

| Score | Action |
|---|---|
| < 65 | Rejet immédiat, journalisé en `rejected` |
| 65 à 74 | Shadow only : stocké, pas d'alerte |
| 75 à 82 | Alerte standard (si proba >= 0.75) |
| >= 83 | Alerte forte priorité (si proba >= 0.75) |

### 8.3 Règles de scoring par critère

**Régime (0-20)**
- Régime parfaitement aligné avec le setup : 20
- Régime compatible mais non optimal : 12
- Régime non compatible : 0 (rejet forcé)

**Structure multi-timeframe (0-20)**
- Alignement `4H + 1H + 15M` dans le même sens : 20
- Alignement `4H + 1H` uniquement : 14
- Divergence entre timeframes : 0 à 8

**Qualité du setup (0-15)**
- Toutes conditions du setup remplies : 15
- Conditions partiellement remplies : 5 à 12
- Conditions minimales manquantes : 0 (rejet)

**Localisation technique (0-10)**
- Entrée sur zone de structure majeure ou forte confluence (`EMA/Fib/round level`) : 10
- Entrée sur zone secondaire : 6
- Zone floue ou sans niveau clair : 0 à 3

**Momentum (0-10)**
- RSI cohérent, MACD confirmé, structure claire : 10
- Momentum divergent ou neutre : 3 à 7
- RSI en zone extrême défavorable : 0 à 2

**Volume (0-10)**
- Volume > 2.0x moyenne : 10
- Volume normal (1.2 à 2.0x) : 6
- Volume faible (< 0.8x) : 0 à 3

**Qualité d'entrée (0-5)**
- Zone d'entrée < 0.15 ATR : 5
- Zone d'entrée 0.15 à 0.25 ATR : 3
- Zone d'entrée > 0.25 ATR : 0 (rejet)

**Clarté du stop (0-5)**
- Stop sur niveau technique évident : 5
- Stop sur niveau secondaire : 3
- Stop flou ou trop large : 0 (rejet)

**Ratio R/R (0-5)**
- R/R >= 2.5 : 5
- R/R 2.0 à 2.49 : 4
- R/R < 2.0 : 0 (rejet)

---

## 9. Moteur de probabilité calibrée

### 9.1 Définition du succès

Un signal est gagnant si **T1 est atteint avant le stop** :
- T1 = entrée + 1.5R (long) / entrée - 1.5R (short)
- Stop = niveau d'invalidation technique défini à l'émission

### 9.2 Méthode de calibration

- Méthode : Isotonic Regression (scikit-learn ou implémentation NumPy maison)
- Input : score brut normalisé + features clés (régime, setup type, direction)
- Output : probabilité calibrée [0, 1]
- La calibration isotonic ne s'active que si les données contiennent au minimum :
  - `min_global_samples_for_calibration` au niveau global
  - `min_setup_direction_samples_for_calibration` pour un bucket `setup:direction`
  - au moins un gain et une perte dans l'échantillon concerné
- Tant que ces conditions ne sont pas réunies, la probabilité shadow reste dérivée du score normalisé, sans forcer de calibrateur fragile
- En phase pré-live, la probabilité shadow dérivée du score est comprimée entre un plancher prudent et un plafond inférieur au seuil live afin d'éviter les faux `90%` issus d'un simple score élevé.
- Si un calibrateur isotonic existe mais que les minimums live ne sont pas atteints, son effet est mélangé progressivement à la probabilité shadow selon la maturité des échantillons. Le calibrateur ne peut donc pas écraser brutalement la collecte shadow ni faire tomber sous le seuil shadow un signal dont le score brut est déjà dans la zone de collecte.
- Recalibration périodique : hebdomadaire (ou manuelle depuis Admin)
- Rafraîchissement dynamique : sur chaque bougie `15M` clôturée, si le jeu de signaux change
  (nouveau signal journalisé ou lifecycle résolu), l'état de probabilité et les métriques live sont reconstruits
- Une nouvelle bougie sans changement de signal ne doit pas forcer de recalibration complète

### 9.3 Conditions d'activation du mode live

- L'identite d'un signal est deterministe par bougie `15M` cloturee.
- Les metriques `recent_*`, la derive de calibration et les segments live sont calcules sur les signaux dedupliques dans l'ordre chronologique reel, jamais sur un ordre inverse.

```yaml
min_total_samples_for_live: 75
min_samples_per_setup_direction: 20
min_samples_per_asset_setup_direction: 10
```

Ces minimums live doivent rester prudents mais compatibles avec un bot swing peu frequent. La validation qualitative reste stricte, mais les quotas de comptage et la fenetre walk-forward ne doivent pas exiger plusieurs annees d'observation avant toute activation manuelle.

Si les minimums ne sont pas atteints, le système reste en `shadow_mode` : les signaux sont générés, journalisés, mais aucune alerte Telegram n'est envoyée.

### 9.4 Seuils de probabilité

```yaml
probability_threshold_live: 0.75
probability_threshold_shadow: 0.62
shadow_probability_floor: 0.50
shadow_probability_cap: 0.74
pre_live_calibration_weight_max: 0.35
```

Le seuil `shadow` peut etre plus permissif que le seuil `live` afin d'accelerer la collecte
d'echantillons, sans degrader le garde-fou du mode `live_alert`.

### 9.5 Niveaux d'alerte

| Probabilité | Niveau |
|---|---|
| < 0.62 | Rejeté |
| 0.62 à 0.74 | Shadow uniquement |
| 0.75 à 0.82 | Alerte standard |
| >= 0.83 | Alerte premium |

---

## 10. Gestion du risque et de l'entrée

### 10.1 Zone d'entrée

- Si le setup est configure en `market_on_close`, `emitted_at` et l'identifiant du signal doivent etre derives du close de la bougie `15M` de confirmation.

```yaml
entry_zone_max_width_atr: 0.25
```

- L'entrée est une zone, pas un prix unique
- Si le setup est configuré en `market_on_close`, l'entrée de référence pour le journal est le close de la bougie `15M` de confirmation
- Si le prix s'échappe au-delà de la zone, le signal expire immédiatement
- Pas de poursuite tardive

### 10.2 Stop

Ordre de priorité technique :
1. Sous le swing low (long) / au-dessus du swing high (short)
2. Sous la borne de range / au-dessus de la borne
3. Stop ATR de secours (uniquement si aucun niveau technique propre)

```yaml
min_stop_distance_atr: 0.8
max_stop_distance_atr: 2.2
```

### 10.3 Objectifs

```yaml
target_1_r_multiple: 1.5
target_2_r_multiple: 2.5
target_3_mode: trailing_optional
```

### 10.4 R/R minimal

```yaml
min_rr_for_alert: 1.8
preferred_rr_for_alert: 2.2
```

Le profil swing V1 retient en pratique :

```yaml
min_rr_for_alert: 2.0
preferred_rr_for_alert: 2.5
```

---

## 11. Format d'alerte obligatoire

### 11.1 Champs obligatoires

Chaque alerte technique issue du moteur de strategie doit contenir exactement ces champs, dans cet ordre :

```
BotYo
{SYMBOL}
{LONG | SHORT}
Setup : {setup_type}
Régime : {regime}
Probabilité : {probability}%
Score : {score}/100
Entrée : {entry_low} - {entry_high}
Stop : {stop}
T1 : {target1}
T2 : {target2}
R/R : {rr}
Validité : {validity_hours}h
Annulation : {invalidation_rule}
Émis à : {emitted_at} UTC
```

### 11.2 Exemple conforme

```
BotYo
BTCUSDT
LONG
Setup : Trend continuation
Régime : Bull trend
Probabilité : 78%
Score : 81/100
Entrée : 84250 - 84500
Stop : 82920
T1 : 86700
T2 : 88900
R/R : 2.1
Validité : 12h
Annulation : clôture 4H sous 83100
Émis à : 2024-11-15 14:00 UTC
```

### 11.3 Règles de format

- Probabilité arrondie à l'entier le plus proche
- Entrée, stop, targets : 0 décimale pour BTC, 2 décimales pour ETH/XRP
- R/R : 1 décimale
- Pas d'emoji ni de markdown dans le message Telegram

### 11.4 Alertes externes `whales`

Les alertes `whales` utilisent un format dedie distinct des alertes techniques.

- Les alertes X reprennent strictement la reponse du LLM `GPT-5.4`
- Si la reponse LLM est `PAS PERTINENT`, aucune alerte n'est emise
- Les alertes wallets doivent inclure au minimum :
  - la source ou le wallet concerne
  - la crypto concernee (`BTC`, `ETH`, `XRP`)
  - le signal detecte (`PUMP` ou `DUMP`)
  - la probabilite d'impact en pourcentage
  - une confirmation technique multi-timeframe resumee (`regime`, biais, RSI/MACD/volume) quand les snapshots de marche sont disponibles
  - une probabilite combinee `whales + analyse technique` si cette confirmation est disponible
  - le montant detecte et sa valeur USD approximative
  - l'horodatage UTC
- Les alertes `whales` suivent le mode global :
  - `shadow_live` : journalisation en base sans envoi reel
  - `live_alert` : envoi Telegram reel

---

## 12. Filtres anti-bruit

### 12.1 Cooldowns

```yaml
cooldown_per_asset_same_direction_hours: 12
cooldown_per_asset_opposite_direction_hours: 6
```

### 12.2 Limites simultanées

```yaml
max_active_alerts_total: 3
max_active_alerts_per_asset_direction: 1
```

### 12.3 Filtres qualité automatiques

```yaml
reject_if_data_incomplete: true
reject_if_no_clear_stop: true
reject_if_rr_below_min: true
reject_if_probability_below_threshold: true
reject_if_confirmation_wick_excessive: true   # mèche > 60% de la bougie
reject_if_late_entry: true                    # prix hors zone d'entrée
```

---

## 13. Validité temporelle des alertes

| Setup | Validité |
|---|---|
| Trend continuation | 18h |
| Breakout | 12h |
| Reversal | 16h |
| Range rotation | 16h |

**Expiration automatique si :**
- La zone d'entrée n'est pas touchée dans le délai pour un setup `zone_retest`
- T1 est atteint sans exécution (signal caduc)
- Le stop technique devient incohérent
- Le régime de marché change

---

## 14. Journal et métriques de performance

### 14.1 Données journalisées par signal

- ID unique du signal
- Symbole, direction, setup, régime
- Features brutes (JSON)
- Score, probabilité
- Zone d'entrée, stop, T1, T2
- Horodatage émission, expiration
- Statut final (active / expired / hit_t1 / hit_stop / cancelled)
- Résultat en R (si clôturé)
- Mode (shadow / live)
- Commentaire système
- Alertes externes `whales` stockées dans `external_alerts` avec `event_id`, source, message, metadata et statut d'envoi

### 14.2 Métriques calculées et affichées

- Win rate global
- Win rate par actif
- Win rate par setup
- Win rate par direction (long/short)
- Résultat moyen en R
- Expectancy
- Drawdown théorique
- Taux de faux positifs
- Taux de non-exécution
- Précision réelle du seuil 75%

---

## 15. Modes de fonctionnement

### `backtest`

- Analyse sur données historiques
- Aucune alerte externe
- Résultats stockés en base
- Usage : calibration initiale, validation des setups

### `shadow_live`

- Synchronisation REST de lancement avant ouverture du WebSocket
- Tourne en temps réel sur WebSocket Kraken
- Détecte un flux OHLC silencieux et force une reconnexion automatique par intervalle
- Génère les signaux complets
- Met à jour le lifecycle des signaux à chaque bougie `15M` clôturée
- Rafraîchit l'état de calibration et de readiness live quand un signal nouveau ou résolu modifie la base utile
- N'envoie pas d'alerte Telegram
- Journalise tout
- Execute aussi les taches de fond `whales`, sans envoi Telegram reel
- Mode par défaut au démarrage

### `live_alert`

- Envoi réel sur Telegram
- Activé uniquement manuellement depuis l'Admin
- Nécessite que tous les critères de calibration soient atteints
- Ne peut pas être activé automatiquement par le bot
- Les alertes `whales` suivent ce mode et peuvent etre envoyees si detectees

---

## 16. Interface web

### 16.1 Dashboard (route `/`)

- Statut global du bot (mode, uptime, dernière bougie reçue)
- Horodatage de la dernière resynchronisation de lancement
- Alertes actives (tableau)
- Alertes récentes des 24h (tableau)
- Régime actuel par actif (badge coloré)
- Panneau diagnostic par actif : biais de marché, blocages du régime, état des 4 setups
- Panneau `whales` : mouvements wallets récents, code couleur `rouge` si le seuil strict USD est dépassé, `orange` si le montant s'en approche, `vert` s'il reste sous le seuil
- Panneau santé des sources : statut de `x`, `x_llm`, `btc`, `eth`, `xrp` et couverture historique intraday effectivement disponible
- Le dashboard doit prioriser l'affichage des mouvements `above_threshold` et `near_threshold` pour réduire le bruit visuel, sans supprimer les traces journalisées
- Métriques clés : win rate, expectancy, total signaux
- Vue de confluence par signal : structure, niveaux, momentum, volume, R/R
- Rafraîchissement automatique depuis l'état serveur à l'intervalle configuré
- Interface dashboard bilingue `fr` / `en`, francais par defaut, conservant la langue selectionnee dans les URLs HTMX et via preference navigateur locale

### 16.2 Admin (route `/admin`)

- Formulaire complet éditable pour chaque paramètre de `bot.yaml`
- Rechargement de la config sans redémarrage
- Activation/désactivation des setups
- Modification des seuils de score, probabilité, R/R
- Gestion des modes (shadow/live)
- Bouton test Telegram (envoie un message de test)
- Endpoint local d'arrêt gracieux `POST /admin/shutdown` pour demander la fermeture propre du bot depuis un autre terminal si `Ctrl+C` est capricieux sous Windows

### 16.3 Journal (route `/journal`)

- Liste paginée de tous les signaux (filtrable par actif, setup, statut)
- Détail d'un signal (toutes les features, lifecycle)
- Graphiques Chart.js : win rate dans le temps, distribution R, performance par setup
- Détail lisible de la confluence et du breakdown de score

### 16.4 Règles frontend

- Pas de SPA, pas de React
- HTMX pour les mises à jour partielles (refresh dashboard toutes les 60s)
- Chart.js pour les graphiques (CDN, pas de bundling)
- CSS minimal, pas de framework CSS lourd (Tailwind ou CSS natif)
- Mobile-friendly basique

---

## 17. Configuration complète (`config/bot.yaml`)

```yaml
bot:
  name: "BotYo"
  mode: "alert_only"
  environment: "shadow_live"
  timezone: "UTC"
  trading_style: "swing"
  execution_type: "manual"

markets:
  symbols:
    - "BTCUSDT"
    - "ETHUSDT"
    - "XRPUSDT"
  directions:
    - "long"
    - "short"
  quote_currency: "USDT"

timeframes:
  trend: "4H"
  setup: "1H"
  entry: "15M"

data:
  use_closed_candles_only: true
  historical_lookback_days: 720
  runtime_sync_seconds: 60
  startup_signal_backfill_bars: 96
  warmup_bars:
    "4H": 500
    "1H": 720
    "15M": 720
  indicators:
    ema_fast: 20
    ema_mid: 50
    ema_slow: 200
    rsi_period: 14
    rsi_oversold: 30
    rsi_overbought: 70
    atr_period: 14
    adx_period: 14
    macd_fast: 12
    macd_slow: 26
    macd_signal: 9
    volume_ma_period: 20
    swing_window: 3
    fib_lookback_bars: 120

regime:
  enabled: true
  bull_trend:
    ema50_above_ema200: true
    close_above_ema50: true
    adx_min: 20
  bear_trend:
    ema50_below_ema200: true
    close_below_ema50: true
    adx_min: 20
  range:
    adx_max: 20
  high_volatility_noise:
    atr_price_ratio_max: 0.06
    wick_ratio_max: 0.60
  low_quality_market:
    min_volume_ratio: 0.65
    min_range_atr_ratio: 0.50

setups:
  trend_continuation:
    enabled: true
    allowed_regimes: ["bull_trend", "bear_trend"]
    require_pullback_to_ema: true
    require_fibonacci_confluence: true
    require_entry_confirmation: true
    pullback_tolerance_atr: 0.45
    entry_zone_half_width_atr: 0.07
    entry_execution_policy: "market_on_close"
    entry_confirmation_min_confluence: 2
    validity_hours: 18
  breakout:
    enabled: true
    allowed_regimes: ["bull_trend", "bear_trend", "range"]
    min_compression_bars: 6
    min_volume_ratio_breakout: 2.0
    require_breakout_close: true
    min_clear_space_atr: 1.6
    entry_zone_half_width_atr: 0.07
    entry_execution_policy: "market_on_close"
    entry_confirmation_min_confluence: 2
    validity_hours: 12
  reversal:
    enabled: true
    allowed_regimes: ["bull_trend", "bear_trend", "range"]
    require_major_level_touch: true
    require_structure_break: true
    min_extension_atr: 1.2
    min_rr: 2.0
    prefer_rsi_divergence: true
    entry_zone_half_width_atr: 0.07
    entry_execution_policy: "market_on_close"
    entry_confirmation_min_confluence: 1
    validity_hours: 16
  range_rotation:
    enabled: true
    allowed_regimes: ["range"]
    require_boundary_reaction: true
    max_distance_from_boundary_atr: 0.4
    require_rsi_reversal: true
    entry_zone_half_width_atr: 0.07
    entry_execution_policy: "market_on_close"
    entry_confirmation_min_confluence: 1
    validity_hours: 16

assets:
  BTCUSDT:
    min_volume_ratio: 1.0
    breakout_volume_ratio: 2.0
    fib_confluence_weight: 1.0
  ETHUSDT:
    min_volume_ratio: 1.0
    breakout_volume_ratio: 2.0
    fib_confluence_weight: 0.9
  XRPUSDT:
    min_volume_ratio: 1.2
    breakout_volume_ratio: 2.3
    fib_confluence_weight: 0.7

scoring:
  max_score: 100
  weights:
    regime: 20
    structure: 20
    setup_quality: 15
    location: 10
    momentum: 10
    volume: 10
    entry_quality: 5
    stop_quality: 5
    rr_quality: 5
  thresholds:
    reject_below: 63
    shadow_from: 63
    live_from: 75
    priority_from: 83

probability:
  enabled: true
  method: "calibrated_model"
  calibration_method: "isotonic"
  probability_threshold_live: 0.75
  probability_threshold_shadow: 0.62
  shadow_probability_floor: 0.50
  shadow_probability_cap: 0.74
  pre_live_calibration_weight_max: 0.35
  success_definition:
    type: "target1_before_stop"
    target1_r_multiple: 1.5
  recalibration_frequency: "weekly"
  min_global_samples_for_calibration: 20
  min_setup_direction_samples_for_calibration: 8
  min_total_samples_for_live: 75
  min_samples_per_setup_direction: 20
  min_samples_per_asset_setup_direction: 10
  live_requirements:
    recent_window: 30
    min_global_expectancy: 0.10
    min_segment_expectancy: 0.10
    min_recent_expectancy: 0.00
    min_recent_win_rate: 45.0
    max_non_execution_rate: 55.0
    max_drawdown_r: 8.0
    max_recent_calibration_gap: 0.12
    min_walk_forward_expectancy: 0.00
    max_walk_forward_brier: 0.30
    walk_forward_folds: 3
    walk_forward_min_records: 45
    walk_forward_min_train_records: 30
    walk_forward_min_test_records_per_fold: 5
    top_segments_live: 4

risk:
  min_rr_for_alert: 2.0
  preferred_rr_for_alert: 2.5
  min_stop_distance_atr: 0.8
  max_stop_distance_atr: 2.2
  entry_zone_max_width_atr: 0.25
  targets:
    t1_r: 1.5
    t2_r: 2.5
    t3_mode: "optional_trailing"

alerts:
  channel: "telegram"
  telegram_bot_token_env: "BOTYO_TELEGRAM_BOT_TOKEN"
  telegram_chat_id_env: "BOTYO_TELEGRAM_CHAT_ID"
  telegram_bot_token: ""
  telegram_chat_id: ""
  include_score: true
  include_probability: true
  include_regime: true
  include_invalidation_rule: true
  cooldown_per_asset_same_direction_hours: 12
  cooldown_per_asset_opposite_direction_hours: 6
  max_active_alerts_total: 3
  max_active_alerts_per_asset_direction: 1

filters:
  reject_if_data_incomplete: true
  reject_if_no_clear_stop: true
  reject_if_rr_below_min: true
  reject_if_probability_below_threshold: true
  reject_if_confirmation_wick_excessive: true
  reject_if_late_entry: true

logging:
  enabled: true
  store_features: true
  store_scores: true
  store_probabilities: true
  store_alert_lifecycle: true
  log_file: "data/logs/botyo.log"
  log_level: "INFO"
  metrics:
    - "win_rate_global"
    - "win_rate_by_asset"
    - "win_rate_by_setup"
    - "win_rate_by_direction"
    - "avg_r_multiple"
    - "expectancy"
    - "false_positive_rate"
    - "non_execution_rate"

modes:
  available:
    - "backtest"
    - "shadow_live"
    - "live_alert"
  default: "shadow_live"
  live_activation_requirements:
    min_total_samples: 75
    min_setup_direction_samples: 20
    require_stable_shadow_results: true

web:
  host: "127.0.0.1"
  port: 8000
  dashboard_refresh_seconds: 60
  journal_page_size: 50

whales:
  enabled: true
  env_file: ".env"
  x:
    enabled: true
    accounts_doc: "whales/X.md"
    api_base_url: "https://api.x.com/2"
    bearer_token_env: "BOTYO_X_BEARER_TOKEN"
    poll_seconds: 45
    max_posts_per_account: 5
    llm_model: "gpt-5.4"
    openai_api_key_env: "OPENAI_API_KEY"
    openai_endpoint: "https://api.openai.com/v1/responses"
  wallets:
    enabled: true
    wallets_doc: "whales/WW.md"
    strict_min_usd_trigger: 10000000
    btc_api_base_url: "https://blockstream.info/api"
    btc_poll_seconds: 30
    eth_api_key_env: "BOTYO_ALCHEMY_API_KEY"
    eth_websocket_url: "wss://eth-mainnet.g.alchemy.com/v2/{api_key}"
    xrp_websocket_url: "wss://xrplcluster.com"
```

---

## 18. Sécurité fonctionnelle

### Garde-fous obligatoires

- Aucune alerte sans stop technique valide
- Aucune alerte sans délai de validité
- Aucune alerte si la donnée est incomplète
- Aucune alerte si l'invalidation est floue
- Aucune alerte si le R/R est insuffisant
- Aucune alerte si la proba est estimée mais non calibrée
- Le mode `live_alert` ne peut être activé qu'explicitement par l'utilisateur depuis l'Admin

### Dégradation contrôlée

Si un module tombe :
- Pas de mode dégradé silencieux
- L'actif concerné est suspendu
- Log d'erreur `ERROR` obligatoire
- Reprise automatique uniquement après validation des données au redémarrage du cycle

### Garde-fous `whales`

- Aucune alerte X si la reponse LLM est `PAS PERTINENT`
- Aucune alerte wallet si le montant USD ne depasse pas `whales.wallets.strict_min_usd_trigger`
- Aucune méta-alerte `wallet_trend` si le biais `24h` reste sous le seuil strict ou si la dominance est insuffisante
- Les alertes `whales` doivent etre dedupliquees par `event_id`
- Une erreur client permanente `4xx` hors `429` ouvre un circuit breaker sur la source externe concernee et son statut doit etre visible sur le dashboard
- `backtest` n'emet aucune alerte externe
- `shadow_live` journalise les alertes `whales` sans envoi Telegram reel
- `live_alert` peut envoyer les alertes `whales` si detectees
- Exception: la méta-alerte `wallet_trend` peut etre forcee vers Telegram meme hors `live_alert`

### Credentials

Runtime actuel prioritaire :

- `BOTYO_TELEGRAM_BOT_TOKEN` et `BOTYO_TELEGRAM_CHAT_ID` sont charges depuis `.env`
- `BOTYO_X_BEARER_TOKEN`, `OPENAI_API_KEY` et `BOTYO_ALCHEMY_API_KEY` sont charges depuis `.env`
- `.env` ne doit jamais etre versionne

- `bot.yaml` ne doit contenir que les noms de variables d'environnement, jamais les secrets eux-memes
