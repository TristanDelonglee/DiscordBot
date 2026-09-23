# Bot Reaction-Role avec proposition de jeux modérée

Un bot Discord qui gère des rôles de jeux via réactions, avec un système où les
membres **proposent** un jeu et où un **modérateur valide** (et choisit l'emoji).
La validation humaine remplace tout matching automatique : c'est le modo qui évite
les doublons (Overwatch 1 / 2 → un seul rôle), corrige les fautes et les abréviations.

## 1. Créer l'application Discord

1. https://discord.com/developers/applications → **New Application**.
2. Onglet **Bot** → **Reset Token** → copie le token.
3. Dans **Bot**, active les deux *Privileged Gateway Intents* :
   - **Server Members Intent** (pour attribuer les rôles)
   - **Message Content Intent** (pour lire les réponses en DM)

## 2. Inviter le bot

Onglet **OAuth2 > URL Generator** :
- Scopes : `bot` + `applications.commands`
- Permissions : **Manage Roles**, **Manage Messages**, **Send Messages**, **Add Reactions**, **Read Message History**

> *Manage Messages* sert à retirer la réaction ➕ d'un membre après son clic (pour
> garder le compteur propre) et à réordonner les réactions via `/rr_resync`.

> ⚠️ Dans **Paramètres du serveur > Rôles**, place le rôle du bot **au-dessus** des
> rôles de jeux qu'il crée, sinon il ne pourra pas les attribuer.

## 3. Lancer

```bash
pip install -r requirements.txt
cp .env.example .env      # puis colle ton token dans .env
python bot.py
```

Pour un serveur de test, mets son ID dans `TEST_GUILD_ID` (`.env`) : les commandes
`/` apparaissent tout de suite. (Activer le *Mode développeur* dans Discord pour
copier les IDs : Paramètres > Avancés.)

## 4. Configurer dans Discord

Dans le salon où tu veux le menu :

```
/rr_setup mod_channel:#nom-du-salon-modo
```

Le bot poste le message reaction-role (avec 5 jeux par défaut + la réaction ➕) et
enverra les demandes d'approbation dans le salon modo indiqué.

Autres commandes (modos) :
- `/rr_addgame nom: emoji:` — ajoute un jeu directement
- `/rr_removegame nom:` — retire un jeu et supprime son rôle
- `/rr_resync` — réordonne les réactions pour suivre l'ordre du texte (➕ en dernier)
- `/rr_list` — liste les jeux

## Ordre des réactions

Discord fige l'ordre des réactions sur le moment où chaque emoji a été ajouté pour
la première fois, et ne permet pas de les réordonner (ni à un bot de réagir à la
place d'un membre). Le bot affiche donc les jeux **dans l'ordre d'ajout** (le texte
suit alors l'ordre des réactions) et repousse automatiquement le ➕ en dernier à
chaque nouveau jeu. Lance `/rr_resync` une fois pour réaligner un message déjà
existant.

## Comment ça marche

1. Un membre clique sur **➕** → le bot lui demande le jeu en DM.
2. Le bot poste la proposition dans le salon modo avec **Accepter / Refuser / Modifier le nom**.
3. **Accepter** → le modo réagit avec l'emoji voulu → le bot crée le rôle (sans
   permissions), met à jour le message, attribue le rôle au proposeur et le prévient.
4. **Modifier** → le modo tape le nom corrigé, puis le flux d'acceptation continue.
5. **Refuser** → le proposeur est prévenu, rien d'autre.
6. Si un message atteint **20 réactions**, un message « Jeux supplémentaires » est créé.

Garde-fou : si le jeu proposé porte le même nom qu'un rôle existant, le bot
n'en crée pas de second et attribue simplement le rôle existant.

## Limites connues

- Une approbation en attente est perdue si le bot redémarre (boutons inactifs).
  Suffisant pour un usage normal ; à durcir avec des *vues persistantes* si besoin.
- L'anti-doublon est basé sur le nom exact (insensible à la casse). Je peux ajouter
  un **avertissement de ressemblance** (fuzzy) au modo si tu le souhaites.
