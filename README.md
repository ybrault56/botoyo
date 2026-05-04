# BotYo

Bootstrap Phase 0 du projet BotYo V1 conformement a `AGENTS.md` et `DEVBOOK.md`.

## Synchronisation au lancement et live

BotYo ne suppose pas que le bot tourne 24h/24.

Au lancement en `shadow_live` ou `live_alert`, le comportement attendu est :

1. resynchroniser la base locale via Kraken REST avant d'ouvrir le flux live ;
2. reprendre depuis la derniere bougie stockee avec un petit recouvrement pour corriger les coupures courtes ;
3. recalculer immediatement les regimes, le lifecycle des signaux et les signaux courants ;
4. ouvrir ensuite le flux Kraken WebSocket pour recevoir les bougies cloturees en temps reel ;
5. forcer une reconnexion automatique si un flux OHLC reste silencieux au-dela de son intervalle attendu ;
6. relancer en plus une resynchronisation REST periodique toutes les `60` secondes pendant l'execution pour rattraper les bougies cloturees si le flux live prend du retard ;
7. rafraichir automatiquement le dashboard depuis l'etat serveur.

Points importants :

- Kraken fournit ici un flux **WebSocket**, pas un webhook.
- Kraken ne fournit pas de "signaux" BotYo ; le bot recupere des **bougies OHLC**, met a jour SQLite, puis recalcule ses propres signaux.
- Le dashboard se met a jour automatiquement selon `web.dashboard_refresh_seconds`.
- La sync runtime periodique est reglee via `data.runtime_sync_seconds` dans `config/bot.yaml` et vaut `60` secondes par defaut.

## Lancement et arret sous PowerShell

Depuis la racine du projet :

```powershell
py -3.12 app/main.py
```

Arret :

```powershell
Ctrl+C
```

Le bot doit s'arreter proprement sur `Ctrl+C` dans PowerShell :

- fermeture du serveur HTTP ;
- arret du supervisor et des taches de fond ;
- fermeture des connexions WebSocket ;
- flush de la file d'ecriture SQLite ;
- retour au prompt PowerShell une fois les ressources BotYo fermees.

Si le `Ctrl+C` du terminal Windows reste capricieux selon l'hote console, utiliser un autre PowerShell pour demander un arret gracieux :

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/shutdown
```

Cette route locale demande au serveur de s'arreter proprement :

- reponse attendue : `status = ok`
- puis fermeture du serveur HTTP
- puis `supervisor stopping` / `supervisor stopped` dans le log
- puis retour au prompt du shell qui executait le bot

## Tache 9 - Procedure Telegram pour les alertes live

BotYo n'envoie pas d'alerte vers un numero de telephone brut. Telegram Bot API envoie les messages vers un `chat_id`.
Pour recevoir les alertes sur le compte Telegram associe a votre numero, il faut donc :

1. Installer Telegram sur le telephone cible et se connecter avec le compte associe a votre numero.
2. Creer un bot Telegram avec `@BotFather`.
   - Ouvrir Telegram.
   - Chercher `@BotFather`.
   - Envoyer `/newbot`.
   - Choisir un nom puis un username finissant par `bot`.
   - Recuperer le `telegram_bot_token` retourne par BotFather.
3. Ouvrir une conversation avec ce bot depuis le compte Telegram qui doit recevoir les alertes.
   - Chercher le bot cree.
   - Envoyer `/start`.
   - Cette etape est obligatoire pour que le bot puisse vous ecrire en message prive.
4. Recuperer le `chat_id` du compte Telegram cible.
   - Dans un navigateur, appeler :
   - `https://api.telegram.org/bot<VOTRE_BOT_TOKEN>/getUpdates`
   - Dans la reponse JSON, lire `message.chat.id`.
   - Si `getUpdates` est vide, renvoyer `/start` au bot puis relancer l'URL.
5. Renseigner BotYo via `.env` avec ces deux valeurs :
   - `BOTYO_TELEGRAM_BOT_TOKEN`
   - `BOTYO_TELEGRAM_CHAT_ID`

Exemple dans `config/bot.yaml` :

```yaml
alerts:
  channel: "telegram"
  telegram_bot_token_env: "BOTYO_TELEGRAM_BOT_TOKEN"
  telegram_chat_id_env: "BOTYO_TELEGRAM_CHAT_ID"
```

Exemple dans `.env` :

```dotenv
BOTYO_TELEGRAM_BOT_TOKEN=123456789:REPLACE_ME
BOTYO_TELEGRAM_CHAT_ID=123456789
```

6. Ne versionnez jamais `.env`.
   - Le repo impose de ne pas commiter de credentials.
7. Lancer BotYo en `shadow_live` pour verifier le pipeline sans envoi reel.
8. Quand la calibration est suffisante, passer en `live_alert` depuis l'Admin.
9. Tester l'envoi Telegram depuis l'endpoint Admin :
   - `POST /admin/telegram/test`
   - Le test n'enverra un message que si `bot.environment = live_alert` et que `BOTYO_TELEGRAM_BOT_TOKEN` + `BOTYO_TELEGRAM_CHAT_ID` sont renseignes.

### Points importants

- Il n'y a pas de "cle API Telegram" separee dans BotYo : la valeur utile est le `telegram_bot_token`.
- Le numero de telephone n'est pas utilise directement par le code.
- Si vous preferez recevoir les alertes dans un groupe Telegram, ajoutez le bot au groupe puis recuperez le `chat_id` du groupe via `getUpdates`.
- Tant que BotYo reste en `shadow_live`, aucun message Telegram reel n'est envoye.

## Module whales

BotYo peut aussi surveiller deux sources externes dans le module `whales` :

1. les posts X des comptes listes dans `whales/X.md` ;
2. les mouvements des wallets listes dans `whales/WW.md`.

Configuration locale recommandee :

1. Copier `.env.example` vers `.env`.
2. Renseigner :
   - `BOTYO_X_BEARER_TOKEN`
   - `OPENAI_API_KEY`
   - `BOTYO_ALCHEMY_API_KEY`
3. Laisser `whales.enabled: true` dans `config/bot.yaml`.

Comportement :

- les posts X sont relus via GPT-5.4 avant emission ;
- les wallets BTC, ETH et XRP declenchent une alerte uniquement au-dessus du seuil strict `whales.wallets.strict_min_usd_trigger` ;
- si les mouvements wallets sur `24h` glissantes dessinent un biais whales clair sur un actif, BotYo emet aussi une meta-alerte Telegram `Whale trend 24h` ;
- le dashboard affiche aussi les mouvements wallets recents avec un code couleur : rouge au-dessus du seuil, orange a proximite du seuil, vert sous le seuil ;
- les alertes unitaires `wallet_movement` respectent le mode runtime global : en `shadow_live`, elles sont journalisees mais non envoyees ; en `live_alert`, elles sont envoyees.
- la meta-alerte `wallet_trend` est forcee vers Telegram des qu'un biais `24h` clair est detecte.
