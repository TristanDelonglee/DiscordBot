# Déployer le bot sur Oracle Cloud (Always Free, 24/7)

Objectif : faire tourner le bot en permanence sur une VM Linux gratuite, qui
redémarre toute seule en cas de crash ou de reboot. Compte ~30 min la première fois.

## Ce qu'il te faut
- Les fichiers du bot : `bot.py`, `requirements.txt`, et ton token Discord.
- Une carte bancaire (Oracle l'exige pour **vérifier ton identité** — tu n'es pas
  facturé tant que tu restes dans les ressources « Always Free »).
- Un terminal avec `ssh` (intégré sous Linux/macOS et Windows 10+).

---

## Étape 1 — Créer le compte Oracle Cloud

1. Va sur https://www.oracle.com/cloud/free/ → **Start for free**.
2. Renseigne email, pays, etc.
3. **Région d'hébergement (home region)** : choisis une région proche, p. ex.
   `France Central (Paris)` ou `Germany Central (Frankfurt)`.
   ⚠️ **Ce choix est définitif** et c'est là que vivront tes ressources gratuites.
4. Vérifie le numéro de téléphone, puis ajoute la carte bancaire (vérification).
5. Attends la fin du provisionnement du compte (quelques minutes, tu reçois un email).

---

## Étape 2 — Créer la machine virtuelle

1. Connecte-toi à la console : https://cloud.oracle.com
2. Menu (☰ en haut à gauche) → **Compute** → **Instances** → **Create instance**.
3. **Name** : par ex. `discord-bot`.
4. **Image** : clique **Edit** dans la section *Image and shape* → **Change image** →
   choisis **Canonical Ubuntu** (24.04 ou 22.04). (Par défaut c'est Oracle Linux ;
   Ubuntu est plus familier avec `apt`.)
5. **Shape** : clique **Change shape** →
   - Onglet **Specialty and previous generation** → coche **VM.Standard.E2.1.Micro**
     (badge **Always Free-eligible**, 1 Go de RAM). C'est suffisant et toujours dispo.
   - *(Alternative plus puissante : onglet **Ampere** → `VM.Standard.A1.Flex`,
     règle 1 OCPU / 6 Go. Plus costaud, mais sujet aux erreurs « out of capacity ».)*
6. **Networking** : laisse créer un nouveau VCN, et **garde coché « Assign a public
   IPv4 address »** (nécessaire pour te connecter en SSH).
7. **Add SSH keys** :
   - Si tu n'as pas encore de clé : choisis **Generate a key pair for me** et
     **télécharge la clé privée** (garde-la précieusement, tu en auras besoin).
   - Si tu as déjà une clé SSH : choisis **Paste public keys** et colle ta clé `.pub`.
8. Clique **Create**. Attends que l'état passe à **Running**, puis note l'**IP publique**
   affichée sur la page de l'instance.

> Pas besoin d'ouvrir de port entrant : le bot fait des connexions **sortantes** vers
> Discord. Seul le SSH (port 22) est utilisé, et il est déjà autorisé par défaut.

---

## Étape 3 — Se connecter en SSH

L'utilisateur par défaut des images Ubuntu est `ubuntu`.

**Linux / macOS :**
```bash
chmod 600 /chemin/vers/ta_cle_privee     # une seule fois
ssh -i /chemin/vers/ta_cle_privee ubuntu@TON_IP_PUBLIQUE
```

**Windows (PowerShell) :**
```powershell
ssh -i C:\chemin\vers\ta_cle_privee ubuntu@TON_IP_PUBLIQUE
```
Tape `yes` à la question sur l'empreinte la première fois.

---

## Étape 4 — Préparer l'environnement

Une fois connecté à la VM :

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
mkdir -p ~/discord-bot && cd ~/discord-bot
```

---

## Étape 5 — Envoyer le code du bot

Choisis **une** des deux méthodes.

### Méthode A — `scp` (depuis ton PC)
Sur **ton PC** (pas sur la VM), dans le dossier qui contient `bot.py` :
```bash
scp -i /chemin/vers/ta_cle_privee bot.py requirements.txt ubuntu@TON_IP_PUBLIQUE:~/discord-bot/
```

### Méthode B — Git (pratique pour les mises à jour)
Pousse `bot.py` et `requirements.txt` dans un dépôt Git (**sans** le `.env` !), puis
sur la VM :
```bash
cd ~/discord-bot
git clone https://gitlab.com/ton-compte/ton-repo.git .
```

---

## Étape 6 — Installer les dépendances et configurer le token

Sur la VM, dans `~/discord-bot` :

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Crée le fichier `.env` (jamais commité dans Git) :
```bash
nano .env
```
Colle dedans (remplace par ton vrai token) :
```
DISCORD_TOKEN=ton_token_ici
TEST_GUILD_ID=l_id_de_ton_serveur
```
Enregistre avec **Ctrl+O**, **Entrée**, puis **Ctrl+X** pour quitter.

---

## Étape 7 — Test rapide

```bash
python3 bot.py
```
Tu dois voir une ligne du type `Connecté en tant que ...`. Vérifie sur Discord que
le bot est en ligne. Puis arrête-le avec **Ctrl+C** (on va le passer en service).

---

## Étape 8 — Lancer 24/7 avec systemd (auto-redémarrage)

Crée le service :
```bash
sudo nano /etc/systemd/system/discordbot.service
```
Colle ceci (les chemins supposent `~/discord-bot` pour l'utilisateur `ubuntu`) :
```ini
[Unit]
Description=Bot Discord Reaction Role
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/discord-bot
ExecStart=/home/ubuntu/discord-bot/venv/bin/python3 /home/ubuntu/discord-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
Enregistre (**Ctrl+O**, **Entrée**, **Ctrl+X**), puis active et démarre :
```bash
sudo systemctl daemon-reload
sudo systemctl enable discordbot
sudo systemctl start discordbot
sudo systemctl status discordbot      # doit afficher "active (running)"
```

`WorkingDirectory` garantit que le bot trouve `.env` et écrit `data.json` au bon
endroit — ta config survit donc à tous les redémarrages.

---

## Au quotidien

**Voir les logs en direct :**
```bash
journalctl -u discordbot -f
```

**Mettre à jour le bot** (c'est ça, la « mise à jour » — pas de réinvite Discord !) :
```bash
cd ~/discord-bot
# remplace bot.py (scp depuis ton PC, ou : git pull)
sudo systemctl restart discordbot
```

**Arrêter / redémarrer :**
```bash
sudo systemctl stop discordbot
sudo systemctl restart discordbot
```

---

## À savoir

- Le bot tournant en continu garde le compte « actif ». Oracle peut récupérer un
  compte **totalement inactif pendant 30 jours**, donc connecte-toi à la console de
  temps en temps — sans souci tant que la VM tourne.
- `data.json` reste sur le disque de la VM : ta config (jeux, rôles, messages) est
  conservée entre les redémarrages.
- Si tu changes les permissions du bot sur Discord, fais-le via le lien d'invitation
  OAuth ou en éditant le rôle du bot dans le serveur — **jamais** besoin de bannir.
