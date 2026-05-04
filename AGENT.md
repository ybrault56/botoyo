# AGENT.md - BotYo

Ce fichier aligne la structure attendue du projet avec le contrat de travail present dans `AGENTS.md`.

Avant chaque phase :
- lire `AGENTS.md`
- lire `DEVBOOK.md`
- appliquer les phases dans l'ordre

Le profil analytique operatoire courant est defini dans `DEVBOOK.md` :
- swing court `1-7 jours`
- cascade `4H -> 1H -> 15M`
- structure prioritaire, confluence obligatoire, `R/R` minimum `1:2`
- synchronisation REST au lancement avant reprise du WebSocket live
- resynchronisation REST periodique pendant l'execution, `60` secondes par defaut, pour verifier que les bougies cloturees rentrent bien meme si le flux live se fige
- preservation de l'historique SQLite propre et exposition explicite d'une couverture partielle si Kraken REST intraday ne fournit pas seul `720 jours`
- replay des dernieres bougies `15M` au lancement pour backfiller les signaux manques pendant l'arret
- reconnexion automatique si un flux Kraken OHLC reste silencieux trop longtemps pour son intervalle
- etat de probabilite rafraichi de facon evenementielle sur bougie `15M` cloturee si les signaux evoluent
- signaux identifies de facon deterministe par bougie `15M` cloturee et calibration dedupliquee par evenement
- calibration isotonic active uniquement quand le jeu d'echantillons est assez profond et contient au moins un gain et une perte
- execution de reference V1 `market_on_close` sur la bougie `15M` de confirmation pour les setups configurés comme immediats
- dashboard auto-rafraichi depuis l'etat serveur avec panneau diagnostic des blocages du pipeline
- dashboard avec sante des sources externes et circuit breaker `whales` visible
- dashboard bilingue `fr` / `en`, francais par defaut, avec conservation de la langue dans les refresh HTMX
- module `whales` capable d'emettre une meta-alerte Telegram sur un biais wallets `24h` clair (`Whale trend 24h`)

`AGENTS.md` reste la source de reference operationnelle du projet.
