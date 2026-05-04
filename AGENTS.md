# AGENT.md - BotYo

> Ce fichier est le contrat de travail de l'agent Codex.
> Il doit etre lu en integralite avant toute action.
> Il doit etre relu au debut de chaque phase de developpement.
> Le DEVBOOK.md doit egalement etre consulte a chaque etape.

---

## 1. Identite du projet

**Nom :** BotYo  
**Type :** Bot d'alerte crypto technique, swing trading, catalyseurs externes `whales`  
**Version cible :** V1  
**Environnement d'execution :** Machine Windows locale, process Python unique  
**Mode V1 :** `shadow_live` par defaut, `live_alert` activable manuellement  
**Profil operatoire V1 :** swing court `1-7 jours` avec cascade `4H -> 1H -> 15M`

---

## 2. Mission de l'agent

L'agent Codex doit construire BotYo V1 strictement conforme aux specifications du DEVBOOK.md.

Ses responsabilites sont :

- Lire et respecter AGENT.md et DEVBOOK.md a chaque phase
- Ne jamais inventer de comportement non specifie
- Ne jamais simplifier un composant critique sans le documenter
- Implementer tous les modules decrits dans le DEVBOOK.md
- Maintenir le module `whales` conformement aux listes `whales/X.md` et `whales/WW.md`
- Valider chaque module par des tests avant de passer au suivant
- Signaler toute ambiguite avant de coder, pas apres

---

## 3. Regles de comportement generales

### 3.1 Priorite absolue des specs

Le DEVBOOK.md est la source de verite. En cas de conflit entre une bonne pratique generale et une spec DEVBOOK, la spec DEVBOOK prime toujours.

### 3.2 Interdictions strictes

L'agent ne doit jamais :

- Utiliser pandas dans la boucle chaude de calcul live
- Introduire Docker, Redis, PostgreSQL, React, Next.js
- Ajouter des dependances non listees dans le DEVBOOK sans validation explicite
- Recalculer tous les indicateurs depuis zero a chaque tick
- Envoyer une alerte sans stop valide, sans delai, sans R/R verifie
- Passer en mode `live_alert` sans que les criteres de calibration soient atteints
- Decider seul d'un setup non defini dans le DEVBOOK
- Utiliser une bougie non cloturee pour une decision de signal
- Valider un signal sur un indicateur isole sans confluence structure + niveau + momentum + volume
- Trader contre la structure dominante `4H` hors cas explicitement definis de `reversal` ou `range_rotation`
- Surveiller un compte X ou un wallet hors des listes `whales/X.md` et `whales/WW.md` sans validation explicite
- Envoyer une alerte `whales` wallet sous le seuil strict configure
- Envoyer une alerte X sans validation LLM `GPT-5.4` ou si la reponse est `PAS PERTINENT`

### 3.3 Profil analytique de reference

Le profil analytique V1 a respecter est le suivant :

- Cascade multi-timeframe : `4H` pour la direction et la structure, `1H` pour la confirmation, `15M` pour l'entree et le stop fin
- Priorite absolue a la structure de marche : `HH/HL` pour le bullish, `LH/LL` pour le bearish
- La lecture de structure est stabilisee par une fenetre de swings configurable, `3` par defaut en V1
- RSI `14` : seuils operatoires `30/70`, divergence consideree comme confirmation forte
- EMA `20 / 50 / 200` : support/resistance dynamique, croisement `20/50` et position relative a `EMA200`
- MACD `12-26-9` : confirmation d'elan et lecture d'essoufflement via l'histogramme
- Volume : un breakout serieux doit etre confirme par un volume eleve, `x2` ou plus etant une confirmation forte
- Niveaux techniques : Fibonacci `0.382 / 0.5 / 0.618`, supports/resistances repetes et niveaux ronds
- La confluence prime sur la frequence : mieux vaut `2-3` setups propres par semaine que multiplier les signaux moyens
- `XRP` doit rester filtre plus durement que `BTC` et `ETH`
- Execution de reference V1 : si le setup `15M` est configure en `market_on_close`, le signal est considere executable au close de la bougie de confirmation
- Calibration prudente : l'isotonic ne doit s'activer qu'apres un minimum d'echantillons et la presence d'au moins un gain et une perte

### 3.4 Dependances autorisees

```
fastapi
uvicorn
aiohttp
httpx
orjson
numpy
pyyaml
jinja2
websockets
itsdangerous
```

Aucune autre dependance sans validation explicite.

### 3.5 Style de code

- Python 3.12 strict
- async/await partout dans les modules I/O
- Type hints obligatoires sur toutes les fonctions publiques
- Pas de classe inutile : fonctions pures preferees pour les calculs
- Docstrings concises sur chaque module et chaque fonction publique
- Pas de `print()` dans le code de production : utiliser le logger interne

---

## 4. Structure de projet obligatoire

L'agent doit reproduire cette structure cible :

```
botyo/
  app/
    main.py
    supervisor.py
    market/
      kraken_ws.py
      kraken_rest.py
    strategy/
      regime.py
      setups.py
      scoring.py
      probability.py
    indicators/
      ema.py
      atr.py
      rsi.py
      adx.py
    storage/
      db.py
    alerts/
      telegram.py
    whales/
      parsers.py
      service.py
    web/
      routes_dashboard.py
      routes_admin.py
      routes_journal.py
      templates/
        base.html
        dashboard.html
        admin.html
        journal.html
      static/
        app.js
        style.css
    utils/
      env.py
      json.py
      logging.py
  config/
    bot.yaml
  data/
    botyo.db
    logs/
  tests/
    test_indicators.py
    test_regime.py
    test_setups.py
    test_scoring.py
    test_probability.py
    test_alerts.py
    test_db.py
    test_api.py
    test_whales.py
  whales/
    WW.md
    X.md
  .env.example
  requirements.txt
  README.md
  AGENT.md
  DEVBOOK.md
```

---

## 5. Phases de developpement obligatoires

L'agent doit suivre ces phases dans l'ordre. Aucun saut de phase n'est autorise.

### Phase 0 - Bootstrap

- Creer la structure complete de repertoires
- Initialiser `requirements.txt`
- Creer `config/bot.yaml` avec la configuration complete du DEVBOOK section 17
- Creer les modules `utils/logging.py`, `utils/json.py` et `utils/env.py`
- Creer `storage/db.py` avec schema SQLite complet, WAL active, et table `external_alerts`
- Validation Phase 0 : `python -c "from app.storage.db import init_db; init_db()"` doit s'executer sans erreur

### Phase 1 - Indicateurs

- Implementer tous les indicateurs en NumPy incremental : `ema.py`, `atr.py`, `rsi.py`, `adx.py`
- Chaque indicateur doit fonctionner en mode mise a jour d'une seule bougie
- Validation Phase 1 : `pytest tests/test_indicators.py`

### Phase 2 - Market Data

- Implementer `kraken_rest.py` pour le bootstrap/resync historique
- Implementer `kraken_ws.py` pour le flux WebSocket live
- Au demarrage, resynchroniser la base via REST avant d'ouvrir le flux WebSocket live
- Si la base contient deja des bougies, reprendre depuis la derniere bougie stockee avec un leger recouvrement
- Ne jamais purger un historique SQLite propre deja collecte ; si Kraken REST intraday ne permet pas seul de remonter `720 jours`, conserver l'archive locale et exposer explicitement une couverture partielle
- Pendant l'execution, relancer une resynchronisation REST periodique, `60` secondes par defaut, pour verifier que les bougies cloturees manquantes sont rattrapees meme si le flux live se fige
- Si un flux WebSocket OHLC reste silencieux au-dela de son intervalle attendu, forcer une reconnexion automatique
- Apres la resynchronisation de lancement, rejouer les dernieres bougies `15M` pour backfiller les signaux manques pendant l'arret
- Puis recalculer immediatement l'etat courant avant d'attendre une nouvelle bougie
- Integrer la regle bougies cloturees uniquement pour les decisions
- Validation Phase 2 : connexion WebSocket Kraken reelle, reception de bougies cloturees `15M`, `1H` et `4H` BTC, stockage en base verifie

### Phase 3 - Moteur de strategie

- Implementer `regime.py` : classification `bull_trend` / `bear_trend` / `range` / `high_volatility_noise` / `low_quality_market`
- Implementer `setups.py` : 4 setups (`trend_continuation`, `breakout`, `reversal`, `range_rotation`)
- Implementer `scoring.py` : score pondere 0-100 selon grille DEVBOOK
- Implementer `probability.py` : calibration isotonique, shadow mode si echantillons insuffisants
- L'identite d'un signal doit etre deterministe par bougie `15M` cloturee ; aucun doublon live/replay ne doit polluer la calibration
- Les indicateurs live doivent reposer sur un etat incremental persiste, pas sur un recompute complet depuis zero a chaque analyse
- Rafraichir l'etat de probabilite de facon evenementielle sur bougie `15M` cloturee quand le jeu de signaux change
  (nouveau signal ou changement de lifecycle resolu), sans forcer une recalibration si rien ne change
- Ne pas laisser une calibration minuscule ecraser les probabilites : avant les minimums de calibration, conserver une proba shadow derivee du score
- Les metriques `recent_*` et les segments live doivent toujours etre calcules dans l'ordre chronologique reel des signaux dedupliques
- Validation Phase 3 : `pytest tests/test_regime.py tests/test_setups.py tests/test_scoring.py tests/test_probability.py`

### Phase 4 - Alertes

- Implementer `alerts/telegram.py`
- Les credentials Telegram doivent etre resolus depuis `.env` via noms de variables, jamais commits en clair dans `bot.yaml`
- Format d'alerte technique 100% conforme au template DEVBOOK section 11
- Respect de tous les filtres anti-bruit techniques
- Validation Phase 4 : envoi d'une alerte test en shadow, log uniquement, pas Telegram reel

### Phase 5 - Supervisor et module whales

- Implementer `supervisor.py` : orchestration async de tous les modules
- Gestion des erreurs par module : suspension de l'actif concerne, log d'erreur, pas de mode degrade silencieux
- Implementation des 3 modes : `backtest`, `shadow_live`, `live_alert`
- Integrer le module `whales` avec 2 taches de fond :
  - surveillance des posts X des comptes listes dans `whales/X.md`
  - surveillance des wallets BTC / ETH / XRP listes dans `whales/WW.md`
- Pour X : recuperer les posts via X API, soumettre le contenu a `GPT-5.4`, rejeter si la reponse est `PAS PERTINENT`
- Pour les wallets : declencher uniquement au-dessus du seuil strict USD configure
- Journaliser les alertes externes en base avec deduplication par `event_id`
- Ajouter un circuit breaker par source externe (`x`, `x_llm`, `btc`, `eth`, `xrp`) sur erreur client permanente `4xx` hors `429`, avec statut visible cote dashboard
- Validation Phase 5 : `python app/main.py` demarre sans erreur en mode `shadow_live`

### Phase 6 - Interface web

- Implementer FastAPI + Jinja2 + HTMX
- Dashboard : alertes actives, alertes recentes, statut par actif, regime actuel
- Dashboard : panneau `whales` avec mouvements wallets recents, rouge si seuil depasse, orange si proche du seuil, vert si sous le seuil
- Le dashboard doit se rafraichir automatiquement a partir de l'etat serveur sans rechargement manuel
- Le dashboard doit inclure un panneau diagnostic par actif : regime courant, blocages du pipeline, statut des 4 setups
- Le dashboard doit afficher la sante des sources externes et la couverture historique intraday effectivement disponible
- Admin : tous les parametres `bot.yaml` configurables depuis l'UI
- Journal : liste des signaux, metriques de performance, et visibilite des alertes externes journalisees
- Dashboard et Journal : exposer la confluence du setup (`structure`, `EMA/Fib`, `RSI/MACD`, `volume`, `R/R`)
- Graphiques : Chart.js pour les metriques
- Validation Phase 6 : `pytest tests/test_api.py`

### Phase 7 - Validation finale (build V1)

Voir section 6 ci-dessous.

---

## 6. Protocole de validation finale (Phase 7)

### 6.1 Checklist obligatoire

L'agent doit verifier chaque point. Un point non valide = build non termine.

**Donnees**
- [ ] WebSocket Kraken connecte, bougies recues pour BTC, ETH, XRP sur 4H, 1H, 15M
- [ ] Historique bootstrappe via REST, `720 jours` minimum si la source le permet ; sinon archive locale preservee et couverture partielle exposee
- [ ] Bougies non cloturees exclues des decisions
- [ ] WAL SQLite actif, verifiable par `PRAGMA journal_mode`

**Indicateurs**
- [ ] EMA 20, 50, 200 calculees de facon incrementale
- [ ] ATR 14 correct
- [ ] RSI 14 correct
- [ ] ADX 14 correct
- [ ] Swing highs / lows detectes

**Regime**
- [ ] Classification correcte sur donnees historiques synthetiques
- [ ] `high_volatility_noise` et `low_quality_market` bloquent les alertes

**Setups**
- [ ] Les 4 setups detectent correctement sur donnees de test
- [ ] Un setup detecte avec regime interdit = rejet automatique
- [ ] Aucun signal sur bougie non cloturee
- [ ] Aucun doublon de signal pour un meme `symbol/setup/direction` sur une meme bougie `15M` cloturee
- [ ] Aucun signal contre la structure `4H` sans confirmation explicite du setup autorise

**Scoring**
- [ ] Score 0-100, ponderation conforme DEVBOOK
- [ ] Seuils reject/shadow/live/priority appliques

**Probabilite**
- [ ] Shadow mode actif si echantillons < minimums
- [ ] Calibration isotonique operationnelle si echantillons suffisants
- [ ] Aucune alerte live si proba < 0.75
- [ ] Les fenetres `recent_*` utilisent les signaux les plus recents en ordre chronologique reel

**Alertes techniques**
- [ ] Format complet avec tous champs obligatoires
- [ ] Cooldown respecte
- [ ] Max 3 alertes simultanees respecte
- [ ] Filtres qualite actifs
- [ ] Alerte sans stop rejetee

**Whales**
- [ ] Les comptes X sont charges depuis `whales/X.md`
- [ ] Les wallets sont charges depuis `whales/WW.md`
- [ ] Un post X non pertinent retourne `PAS PERTINENT` et n'est pas alerte
- [ ] Un post X pertinent est analyse par `GPT-5.4` avant emission
- [ ] Un mouvement wallet inferieur au seuil strict n'emet aucune alerte
- [ ] Les alertes `whales` sont dedupliquees et journalisees en base
- [ ] Une erreur client permanente `4xx` hors `429` suspend la source externe concernee et remonte cet etat au dashboard

**Web**
- [ ] Dashboard accessible sur `http://localhost:8000`
- [ ] Admin modifie `bot.yaml` et rechargement sans redemarrage
- [ ] Journal affiche les metriques correctement
- [ ] Toutes les routes FastAPI retournent 200 sur donnees de test

**Modes**
- [ ] `shadow_live` : signaux generes, non envoyes sur Telegram, journalises
- [ ] `live_alert` : envoi Telegram reel si token configure et calibration atteinte
- [ ] `backtest` : analyse historique, pas d'alerte externe

### 6.2 Protocole de correction automatique

Si un point de la checklist echoue :

1. L'agent identifie le module responsable
2. Il relit la section DEVBOOK correspondante
3. Il corrige le module
4. Il relance tous les tests du module concerne
5. Il relance la checklist complete
6. Il ne declare pas le build termine tant qu'un seul point echoue

### 6.3 Declaration de fin de build

Le build V1 est termine lorsque :

- `pytest tests/` : 0 erreur, 0 failure
- La checklist Phase 7 est integralement cochee
- `python app/main.py` demarre proprement en `shadow_live`
- Le dashboard est accessible et fonctionnel
- Un fichier `BUILD_STATUS.md` est cree a la racine avec : date, resultats des tests, statut de chaque checklist item

---

## 7. Gestion des erreurs et logging

- Tout module qui echoue doit logger l'erreur avec niveau `ERROR`
- L'actif concerne est suspendu, ou la source externe `whales` est suspendue
- Pas de `try/except` silencieux : chaque exception doit etre loggee
- Reprise automatique uniquement apres validation des donnees au redemarrage du cycle

---

## 8. Ce que l'agent ne doit jamais faire

- Simplifier un filtre de qualite pour les tests
- Desactiver la validation du R/R pour debugger plus vite
- Hardcoder un token Telegram ou une cle API dans le code
- Commiter des credentials dans un fichier versionne
- Passer en `live_alert` automatiquement sans intervention humaine
- Modifier les seuils de probabilite sans commenter le changement
- Ignorer une etape de validation sous pretexte qu'elle est evidente

---

## 9. Reference rapide

| Element | Valeur |
|---|---|
| Language | Python 3.12 |
| Paradigme | async/await |
| Source donnees live | Kraken WebSocket v2 |
| Source bootstrap | Kraken REST |
| Sources externes `whales` | X API, Blockstream Esplora, Alchemy WebSocket, XRPL WebSocket |
| LLM social | GPT-5.4 |
| Stockage | SQLite WAL |
| Calcul | NumPy incremental |
| Backend | FastAPI + Uvicorn |
| Frontend | Jinja2 + HTMX + Chart.js |
| Alertes | Telegram Bot API (HTTP direct) |
| Serialisation | orjson |
| Config | YAML + `.env` pour les cles externes |
| Mode defaut | shadow_live |
| Seuil proba live | 0.75 |
| Seuil R/R minimal | 2.0 |
| Actifs | BTCUSDT, ETHUSDT, XRPUSDT |
| Timeframes | 4H (trend), 1H (setup), 15M (entree) |
