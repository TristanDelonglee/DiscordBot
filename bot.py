"""
Bot Discord — Reaction Role avec proposition de jeux modérée.

Fonctionnement :
  • Un (ou plusieurs) message(s) "reaction role" listent des jeux associés à des emojis.
    Réagir = obtenir le rôle ; retirer la réaction = perdre le rôle.
  • Une réaction "➕ Proposer un autre jeu" sur le message principal permet à un membre
    de suggérer un jeu. Le bot lui demande le nom en DM.
  • La proposition est envoyée dans un salon modo avec 3 boutons :
        Accepter / Refuser / Modifier le nom
  • À l'acceptation, le modo choisit un emoji (en réagissant), le bot crée le rôle
    (sans permissions), met à jour le message reaction-role, attribue le rôle au
    proposeur et le prévient en DM.
  • Si la limite Discord de 20 réactions par message est atteinte, un nouveau message
    "Jeux supplémentaires" est créé automatiquement.

Choix par défaut : approbations dans un salon modo dédié, défini via /rr_setup.
"""

import os
import json
import time
import shutil
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from keep_alive import keep_alive

# ----------------------------------------------------------------------------- #
#  Configuration
# ----------------------------------------------------------------------------- #
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")  # optionnel : sync instantané sur un serveur de test
DATA_FILE = os.getenv("DATA_FILE", "data.json")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))  # nb de copies conservées
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL")  # ex: https://hc-ping.com/xxxxx-uuid
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "10"))

PROPOSE_EMOJI = "\u2795"  # ➕

# Jeux pré-remplis au /rr_setup (éditable). emoji unicode pour rester portable.
DEFAULT_GAMES = {
    "Overwatch": "\U0001F6E1\uFE0F",        # 🛡️
    "Minecraft": "\u26CF\uFE0F",            # ⛏️
    "Valorant": "\U0001F52B",               # 🔫
    "League of Legends": "\u2694\uFE0F",    # ⚔️
    "Rocket League": "\U0001F697",          # 🚗
}

REACTION_LIMIT = 20  # limite Discord par message
DM_TIMEOUT = 300      # secondes pour répondre en DM
EMOJI_TIMEOUT = 120   # secondes pour que le modo choisisse l'emoji

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rrbot")


# ----------------------------------------------------------------------------- #
#  Persistance (JSON simple)
# ----------------------------------------------------------------------------- #
class Store:
    """Stocke la config par serveur dans un fichier JSON.

    Structure :
    {
      "guilds": {
        "<guild_id>": {
          "rr_channel_id": int,         # salon des messages reaction-role
          "mod_channel_id": int,        # salon des approbations
          "messages": [int, ...],       # IDs des messages reaction-role (le 1er = principal)
          "games": {
            "Nom du jeu": {"emoji": str, "role_id": int, "message_id": int}
          }
        }
      }
    }
    """

    def __init__(self, path: str):
        self.path = path
        self.data = {"guilds": {}}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Lecture de %s impossible (%s), on repart à vide.", self.path, exc)
                self.data = {"guilds": {}}
        self.data.setdefault("guilds", {})

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def guild(self, guild_id) -> dict | None:
        """Renvoie la config du serveur, ou None si pas encore configuré."""
        return self.data["guilds"].get(str(guild_id))

    def ensure_guild(self, guild_id) -> dict:
        g = self.data["guilds"].setdefault(
            str(guild_id),
            {
                "rr_channel_id": None, 
                "mod_channel_id": None, 
                "messages": [], 
                "games": {},
                # Nouvelles clés
                "logs_channel_id": None,
                "anniv_channel_id": None,
                "starboard_channel_id": None,
                "auto_thread_channels": [],
                "birthdays": {},
                "starboard_msgs": []
            },
        )
        return g


store = Store(DATA_FILE)


# ----------------------------------------------------------------------------- #
#  Sauvegardes de data.json
# ----------------------------------------------------------------------------- #
def do_backup() -> str | None:
    """Copie data.json dans backups/data-AAAAMMJJ-HHMMSS.json et purge les vieilles copies.
    Renvoie le chemin de la copie, ou None si data.json n'existe pas encore."""
    if not os.path.exists(DATA_FILE):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"data-{time.strftime('%Y%m%d-%H%M%S')}.json")
    shutil.copy2(DATA_FILE, dest)
    copies = sorted(f for f in os.listdir(BACKUP_DIR)
                    if f.startswith("data-") and f.endswith(".json"))
    for old in copies[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return dest


@tasks.loop(hours=24)
async def auto_backup():
    dest = do_backup()
    if dest:
        log.info("Sauvegarde automatique : %s", dest)


@auto_backup.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


@tasks.loop(minutes=HEARTBEAT_MINUTES)
async def heartbeat():
    """Ping healthchecks.io. Si le bot tombe, healthchecks n'entend plus rien et alerte."""
    if not HEALTHCHECK_URL:
        return
    import aiohttp  # fourni par discord.py, pas d'installation en plus
    try:
        async with aiohttp.ClientSession() as s:
            await s.get(HEALTHCHECK_URL, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as exc:
        log.warning("Heartbeat échoué (%s) — sans conséquence pour le bot.", exc)


@heartbeat.before_loop
async def _wait_ready_hb():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------------- #
#  Helpers
# ----------------------------------------------------------------------------- #
def used_emojis(gdata: dict) -> set[str]:
    """Emojis déjà associés à un jeu (pour éviter les doublons)."""
    return {g["emoji"] for g in gdata["games"].values()}


async def safe_dm(member: discord.Member | None, text: str):
    """Envoie un DM en ignorant les erreurs (DM fermés, membre parti...)."""
    if member is None:
        return
    try:
        await member.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def rebuild_message(bot: commands.Bot, guild_id: int, message_id: int):
    """Régénère le contenu d'un message reaction-role à partir de l'état stocké."""
    gdata = store.guild(guild_id)
    if not gdata:
        return
    channel = bot.get_channel(gdata["rr_channel_id"])
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    is_main = gdata["messages"] and gdata["messages"][0] == message_id
    # Ordre d'ajout (insertion dict) : il correspond ainsi à l'ordre des réactions,
    # que Discord fige sur la 1re fois où chaque emoji a été ajouté.
    here = [(n, d) for n, d in gdata["games"].items() if d["message_id"] == message_id]
    lines = [f"{d['emoji']} — {n}" for n, d in here]

    if is_main:
        body = (
            "**\U0001F3AE  Choisis tes jeux**\n"
            "Réagis avec l'emoji pour obtenir le rôle correspondant.\n\n"
            + ("\n".join(lines) if lines else "_(aucun jeu pour l'instant)_")
            + f"\n\n{PROPOSE_EMOJI} — **Proposer un autre jeu**"
        )
    else:
        body = "**\U0001F3AE  Jeux supplémentaires**\n\n" + "\n".join(lines)
    await msg.edit(content=body)


async def ensure_capacity(bot: commands.Bot, guild_id: int) -> int:
    """Renvoie l'ID d'un message ayant de la place pour un emoji de plus,
    en créant un message "supplémentaire" si tout est plein."""
    gdata = store.guild(guild_id)
    channel = bot.get_channel(gdata["rr_channel_id"])
    for mid in gdata["messages"]:
        is_main = gdata["messages"][0] == mid
        used = sum(1 for d in gdata["games"].values() if d["message_id"] == mid)
        budget = REACTION_LIMIT - (1 if is_main else 0)  # le principal réserve 1 slot pour ➕
        if used < budget:
            return mid
    new_msg = await channel.send("**\U0001F3AE  Jeux supplémentaires**")
    gdata["messages"].append(new_msg.id)
    store.save()
    return new_msg.id


async def place_reaction(msg: discord.Message, emoji_key: str, is_main: bool, bot_user):
    """Ajoute la réaction d'un jeu en gardant ➕ en dernier sur le message principal.

    Astuce : on retire la réaction ➕ du bot puis on la ré-ajoute après le nouvel
    emoji. Comme le bot est le seul à porter ➕ (les membres voient leur réaction ➕
    retirée juste après le clic), Discord lui attribue un nouvel horodatage et la
    repousse en fin de liste — sans toucher aux réactions des membres.
    """
    if is_main:
        try:
            await msg.remove_reaction(PROPOSE_EMOJI, bot_user)
        except discord.HTTPException:
            pass
    await msg.add_reaction(discord.PartialEmoji.from_str(emoji_key))
    if is_main:
        await msg.add_reaction(PROPOSE_EMOJI)


async def get_or_create_role(guild: discord.Guild, name: str) -> discord.Role:
    """Renvoie le rôle portant ce nom s'il existe déjà, sinon le crée.
    Évite les doublons même si le bot est réinvité ou /rr_setup relancé."""
    existing = discord.utils.get(guild.roles, name=name)
    if existing is not None:
        return existing
    return await guild.create_role(
        name=name,
        permissions=discord.Permissions.none(),
        mentionable=True,
        reason="Rôle de jeu (reaction-role)",
    )


async def add_game(bot: commands.Bot, guild: discord.Guild, name: str, emoji_key: str,
                   proposer: discord.Member | None) -> discord.Role:
    """Crée (ou réutilise) le rôle, l'ajoute à un message reaction-role et l'attribue au proposeur."""
    gdata = store.guild(guild.id)
    role = await get_or_create_role(guild, name)
    msg_id = await ensure_capacity(bot, guild.id)
    gdata["games"][name] = {"emoji": emoji_key, "role_id": role.id, "message_id": msg_id}
    store.save()

    await rebuild_message(bot, guild.id, msg_id)
    channel = bot.get_channel(gdata["rr_channel_id"])
    msg = await channel.fetch_message(msg_id)
    is_main = gdata["messages"][0] == msg_id
    await place_reaction(msg, emoji_key, is_main, bot.user)

    if proposer is not None:
        try:
            await proposer.add_roles(role, reason="Proposition acceptée")
        except (discord.Forbidden, discord.HTTPException):
            pass
    return role


async def capture_emoji(bot: commands.Bot, prompt: discord.Message,
                        approver_id: int, gdata: dict) -> str | None:
    """Attend que le modo réagisse au message `prompt` avec l'emoji à associer.
    Vérifie que l'emoji n'est pas déjà pris. Renvoie la clé emoji, ou None si timeout."""
    def check(payload: discord.RawReactionActionEvent) -> bool:
        if payload.message_id != prompt.id or payload.user_id != approver_id:
            return False
        if payload.member and payload.member.bot:
            return False
        return True

    for _ in range(5):
        try:
            payload = await bot.wait_for("raw_reaction_add", timeout=EMOJI_TIMEOUT, check=check)
        except asyncio.TimeoutError:
            return None
        key = str(payload.emoji)
        if key == PROPOSE_EMOJI:
            await prompt.channel.send("➕ est réservé. Choisis un autre emoji.")
            continue
        if key in used_emojis(gdata):
            await prompt.channel.send(f"L'emoji {key} est déjà utilisé par un autre jeu. Réessaie.")
            continue
        return key
    return None


# ----------------------------------------------------------------------------- #
#  Flux de proposition (déclenché par la réaction ➕)
# ----------------------------------------------------------------------------- #
async def handle_proposal(bot: commands.Bot, guild: discord.Guild, member: discord.Member):
    gdata = store.guild(guild.id)
    if not gdata or not gdata.get("mod_channel_id"):
        return
    mod_channel = bot.get_channel(gdata["mod_channel_id"])
    rr_channel = bot.get_channel(gdata["rr_channel_id"])

    # 1) Demander le jeu en DM
    try:
        dm = await member.create_dm()
        await dm.send(
            "Salut ! Quel jeu aimerais-tu proposer d'ajouter ?\n"
            "Réponds-moi simplement avec le **nom du jeu** (ou « annuler »)."
        )
    except (discord.Forbidden, discord.HTTPException):
        warn = await rr_channel.send(
            f"{member.mention} je n'arrive pas à t'écrire en privé — "
            "ouvre tes DMs puis reclique sur ➕."
        )
        await asyncio.sleep(15)
        try:
            await warn.delete()
        except discord.HTTPException:
            pass
        return

    # 2) Attendre la réponse
    def dm_check(m: discord.Message) -> bool:
        return m.author.id == member.id and isinstance(m.channel, discord.DMChannel)

    try:
        reply = await bot.wait_for("message", timeout=DM_TIMEOUT, check=dm_check)
    except asyncio.TimeoutError:
        await safe_dm(member, "Temps écoulé, proposition annulée. Reclique sur ➕ pour réessayer.")
        return

    name = reply.content.strip()
    if not name or name.lower() in ("annuler", "cancel"):
        await dm.send("Proposition annulée.")
        return

    # 3) Envoyer la demande d'approbation au salon modo
    embed = discord.Embed(
        title="Nouvelle proposition de jeu",
        description=f"**{member}** ({member.mention}) propose :\n\n# {name}",
        color=discord.Color.blurple(),
    )
    existing = ", ".join(sorted(gdata["games"].keys())) or "(aucun)"
    embed.add_field(name="Jeux déjà présents", value=existing[:1024], inline=False)
    embed.set_footer(text="Accepter → tu choisiras l'emoji • Modifier → corriger le nom")

    await mod_channel.send(embed=embed, view=ApprovalView(member.id, name))
    await dm.send("Merci ! Ta proposition a été transmise aux modérateurs. \u2705")


# ----------------------------------------------------------------------------- #
#  Acceptation (commune aux boutons Accepter et Modifier)
# ----------------------------------------------------------------------------- #
async def process_acceptance(interaction: discord.Interaction, proposer_id: int, name: str):
    bot: commands.Bot = interaction.client
    guild = interaction.guild
    gdata = store.guild(guild.id)
    name = name.strip()
    proposer = guild.get_member(proposer_id)

    # Garde-fou anti-doublon : même nom (insensible à la casse) déjà présent.
    existing = next((n for n in gdata["games"] if n.lower() == name.lower()), None)
    if existing:
        role = guild.get_role(gdata["games"][existing]["role_id"])
        if proposer and role:
            try:
                await proposer.add_roles(role, reason="Jeu déjà existant")
            except (discord.Forbidden, discord.HTTPException):
                pass
        await safe_dm(proposer, f"Le jeu « {existing} » existait déjà — je t'ai attribué le rôle. \u2705")
        await interaction.followup.send(
            f"\u26A0\uFE0F « {name} » correspond au jeu existant « {existing} ». "
            "Rôle attribué au proposeur, aucun doublon créé.",
            ephemeral=True,
        )
        return

    # Choix de l'emoji par le modo
    prompt = await interaction.channel.send(
        f"{interaction.user.mention} réagis à **ce message** avec l'emoji à associer à « {name} »."
    )
    emoji_key = await capture_emoji(bot, prompt, interaction.user.id, gdata)
    try:
        await prompt.delete()
    except discord.HTTPException:
        pass
    if emoji_key is None:
        await interaction.followup.send("Aucun emoji reçu à temps, ajout annulé.", ephemeral=True)
        return

    # Création du rôle + mise à jour du message reaction-role + attribution
    try:
        role = await add_game(bot, guild, name, emoji_key, proposer)
    except discord.Forbidden:
        await interaction.followup.send(
            "Je n'ai pas les permissions pour créer le rôle. Vérifie que j'ai **Gérer les rôles** "
            "et que mon rôle est assez haut dans la hiérarchie.",
            ephemeral=True,
        )
        return

    await safe_dm(proposer, f"\u2705 « {name} » a été ajouté et je t'ai donné le rôle **{role.name}** !")
    await interaction.followup.send(f"\u2705 « {name} » ajouté avec l'emoji {emoji_key}.", ephemeral=True)


# ----------------------------------------------------------------------------- #
#  Vue d'approbation (boutons)
# ----------------------------------------------------------------------------- #
class ApprovalView(discord.ui.View):
    def __init__(self, proposer_id: int, name: str):
        super().__init__(timeout=86400)  # 24 h
        self.proposer_id = proposer_id
        self.name = name

    async def _is_mod(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "Seuls les modérateurs (permission *Gérer les rôles*) peuvent décider.",
                ephemeral=True,
            )
            return False
        return True

    async def _disable(self, interaction: discord.Interaction, outcome: str):
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.message.edit(content=outcome, view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_mod(interaction):
            return
        await interaction.response.send_message(
            f"Ok — choisis maintenant l'emoji pour « {self.name} » \U0001F447", ephemeral=True
        )
        await self._disable(interaction, f"\u2705 Accepté par {interaction.user.mention}")
        await process_acceptance(interaction, self.proposer_id, self.name)

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, emoji="\u274C")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_mod(interaction):
            return
        await interaction.response.send_message("Proposition refusée.", ephemeral=True)
        await self._disable(interaction, f"\u274C Refusé par {interaction.user.mention}")
        proposer = interaction.guild.get_member(self.proposer_id)
        await safe_dm(proposer, f"Ta proposition « {self.name} » n'a pas été retenue par les modérateurs.")

    @discord.ui.button(label="Modifier le nom", style=discord.ButtonStyle.secondary, emoji="\u270F\uFE0F")
    async def modify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_mod(interaction):
            return
        await interaction.response.send_message(
            f"Envoie **dans ce salon** le nom corrigé pour « {self.name} ».", ephemeral=True
        )
        await self._disable(interaction, f"\u270F\uFE0F En cours de correction par {interaction.user.mention}")

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

        try:
            msg = await interaction.client.wait_for("message", timeout=DM_TIMEOUT, check=check)
        except asyncio.TimeoutError:
            await interaction.followup.send("Temps écoulé, correction annulée.", ephemeral=True)
            return
        new_name = msg.content.strip()
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        await process_acceptance(interaction, self.proposer_id, new_name)


# ----------------------------------------------------------------------------- #
#  Bot
# ----------------------------------------------------------------------------- #
class RoleBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # privilégié — attribuer les rôles
        intents.message_content = True  # privilégié — lire les réponses en DM
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        if TEST_GUILD_ID:
            g = discord.Object(id=int(TEST_GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            log.info("Slash commands synchronisées sur le serveur de test %s.", TEST_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synchronisées globalement (propagation jusqu'à 1 h).")


bot = RoleBot()


@bot.event
async def on_ready():
    log.info("Connecté en tant que %s (id=%s)", bot.user, bot.user.id)
    if not auto_backup.is_running():
        auto_backup.start()  # 1re copie immédiate, puis toutes les 24 h
    if HEALTHCHECK_URL and not heartbeat.is_running():
        heartbeat.start()
        log.info("Heartbeat healthchecks.io actif (toutes les %s min).", HEARTBEAT_MINUTES)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    gdata = store.guild(payload.guild_id)
    if not gdata:
        return

    guild = bot.get_guild(payload.guild_id)
    member = payload.member

    # --- LOGIQUE STARBOARD ---
    if str(payload.emoji) == "⭐":
        channel = bot.get_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        
        # Supprimer l'auto-réaction (comme MEE6)
        if payload.user_id == msg.author.id:
            await msg.remove_reaction(payload.emoji, member)
            return

        # Vérifier si on atteint 5 étoiles et si le message n'a pas déjà été posté
        star_reaction = discord.utils.get(msg.reactions, emoji="⭐")
        if star_reaction and star_reaction.count >= 5:
            if msg.id not in gdata.get("starboard_msgs", []):
                starboard_channel = bot.get_channel(gdata.get("starboard_channel_id"))
                if starboard_channel:
                    embed = discord.Embed(description=msg.content, color=discord.Color.gold())
                    embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
                    embed.add_field(name="Source", value=f"[Aller au message]({msg.jump_url})")
                    await starboard_channel.send(f"⭐ **5** | {msg.channel.mention}", embed=embed)
                    
                    # Marquer comme posté
                    gdata.setdefault("starboard_msgs", []).append(msg.id)
                    store.save()

    if member is None or member.bot:
        return
    key = str(payload.emoji)

    # ➕ sur le message principal → flux de proposition
    if key == PROPOSE_EMOJI and gdata["messages"][0] == payload.message_id:
        try:
            channel = bot.get_channel(payload.channel_id)
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(PROPOSE_EMOJI, member)  # garder le compteur propre
        except discord.HTTPException:
            pass
        asyncio.create_task(handle_proposal(bot, guild, member))
        return

    # Emoji de jeu → attribuer le rôle
    game = next(
        (d for d in gdata["games"].values()
         if d["message_id"] == payload.message_id and d["emoji"] == key),
        None,
    )
    if game:
        role = guild.get_role(game["role_id"])
        if role:
            try:
                await member.add_roles(role, reason="Reaction role")
            except (discord.Forbidden, discord.HTTPException):
                pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id is None or payload.user_id == bot.user.id:
        return
    gdata = store.guild(payload.guild_id)
    if not gdata or payload.message_id not in gdata.get("messages", []):
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id) if guild else None
    if member is None:
        return
    key = str(payload.emoji)
    game = next(
        (d for d in gdata["games"].values()
         if d["message_id"] == payload.message_id and d["emoji"] == key),
        None,
    )
    if game:
        role = guild.get_role(game["role_id"])
        if role:
            try:
                await member.remove_roles(role, reason="Reaction role retirée")
            except (discord.Forbidden, discord.HTTPException):
                pass


# ----------------------------------------------------------------------------- #
#  Commandes slash (admin)
# ----------------------------------------------------------------------------- #
@bot.tree.command(description="Initialise le message reaction-role dans ce salon.")
@app_commands.describe(mod_channel="Salon où arriveront les demandes d'approbation")
async def rr_setup(interaction: discord.Interaction, mod_channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return

    existing = store.guild(interaction.guild_id)
    if existing and existing.get("messages"):
        await interaction.response.send_message(
            "Ce serveur est déjà configuré. Utilise `/rr_reset` pour repartir de zéro, "
            "ou `/rr_addgame` pour ajouter un jeu.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    main_msg = await interaction.channel.send("Initialisation…")
    gdata = store.ensure_guild(guild.id)
    gdata.update({
        "rr_channel_id": interaction.channel.id,
        "mod_channel_id": mod_channel.id,
        "messages": [main_msg.id],
        "games": {},
    })
    store.save()

    for name, emoji in DEFAULT_GAMES.items():
        role = await get_or_create_role(guild, name)  # réutilise si déjà présent
        gdata["games"][name] = {"emoji": emoji, "role_id": role.id, "message_id": main_msg.id}
    store.save()

    await rebuild_message(bot, guild.id, main_msg.id)
    for emoji in DEFAULT_GAMES.values():
        await main_msg.add_reaction(discord.PartialEmoji.from_str(emoji))
    await main_msg.add_reaction(PROPOSE_EMOJI)

    await interaction.followup.send(
        f"\u2705 Configuré ! Les propositions arriveront dans {mod_channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(description="Réinitialise la config (supprime les messages reaction-role, conserve les rôles).")
async def rr_reset(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("Réservé aux administrateurs.", ephemeral=True)
        return
    gdata = store.guild(interaction.guild_id)
    if not gdata:
        await interaction.response.send_message("Rien à réinitialiser.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(gdata.get("rr_channel_id"))
    if channel:
        for mid in gdata.get("messages", []):
            try:
                msg = await channel.fetch_message(mid)
                await msg.delete()
            except discord.HTTPException:
                pass

    store.data["guilds"].pop(str(interaction.guild_id), None)
    store.save()
    await interaction.followup.send(
        "\u2705 Config réinitialisée. Les rôles existants ont été conservés (et seront "
        "réutilisés, sans doublon, au prochain `/rr_setup`).",
        ephemeral=True,
    )


@bot.tree.command(description="Ajoute directement un jeu (sans passer par une proposition).")
@app_commands.describe(nom="Nom du jeu", emoji="Emoji à associer")
async def rr_addgame(interaction: discord.Interaction, nom: str, emoji: str):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Réservé aux modérateurs.", ephemeral=True)
        return
    gdata = store.guild(interaction.guild_id)
    if not gdata:
        await interaction.response.send_message("Lance d'abord `/rr_setup`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    nom = nom.strip()
    if any(n.lower() == nom.lower() for n in gdata["games"]):
        await interaction.followup.send(f"« {nom} » existe déjà.", ephemeral=True)
        return
    if emoji in used_emojis(gdata) or emoji == PROPOSE_EMOJI:
        await interaction.followup.send("Cet emoji est déjà utilisé. Choisis-en un autre.", ephemeral=True)
        return
    try:
        await add_game(bot, interaction.guild, nom, emoji, None)
    except discord.HTTPException:
        await interaction.followup.send(
            "Emoji invalide ou permissions manquantes (Gérer les rôles).", ephemeral=True
        )
        return
    await interaction.followup.send(f"\u2705 « {nom} » ajouté.", ephemeral=True)


@bot.tree.command(description="Retire un jeu (et supprime son rôle).")
@app_commands.describe(nom="Nom exact du jeu à retirer")
async def rr_removegame(interaction: discord.Interaction, nom: str):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Réservé aux modérateurs.", ephemeral=True)
        return
    gdata = store.guild(interaction.guild_id)
    if not gdata:
        await interaction.response.send_message("Lance d'abord `/rr_setup`.", ephemeral=True)
        return
    key = next((n for n in gdata["games"] if n.lower() == nom.strip().lower()), None)
    if not key:
        await interaction.response.send_message(f"« {nom} » introuvable.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    data = gdata["games"].pop(key)
    store.save()
    role = interaction.guild.get_role(data["role_id"])
    if role:
        try:
            await role.delete(reason="rr_removegame")
        except discord.HTTPException:
            pass
    await rebuild_message(bot, interaction.guild_id, data["message_id"])
    channel = bot.get_channel(gdata["rr_channel_id"])
    try:
        msg = await channel.fetch_message(data["message_id"])
        await msg.clear_reaction(discord.PartialEmoji.from_str(data["emoji"]))
    except discord.HTTPException:
        pass
    await interaction.followup.send(f"\u2705 « {key} » retiré.", ephemeral=True)


@bot.tree.command(description="Réordonne les réactions pour suivre l'ordre du texte (➕ en dernier).")
async def rr_resync(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Réservé aux modérateurs.", ephemeral=True)
        return
    gdata = store.guild(interaction.guild_id)
    if not gdata:
        await interaction.response.send_message("Lance d'abord `/rr_setup`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(gdata["rr_channel_id"])
    for mid in gdata["messages"]:
        is_main = gdata["messages"][0] == mid
        try:
            msg = await channel.fetch_message(mid)
        except discord.HTTPException:
            continue
        # Effacer puis ré-ajouter dans l'ordre d'ajout. Les membres gardent leurs
        # rôles (l'effacement global ne déclenche pas d'événement de retrait par membre),
        # mais leurs réactions sont remises à zéro — d'où l'usage ponctuel.
        try:
            await msg.clear_reactions()
        except discord.Forbidden:
            await interaction.followup.send(
                "Il me manque la permission **Gérer les messages** pour effacer les réactions.",
                ephemeral=True,
            )
            return
        await rebuild_message(bot, interaction.guild_id, mid)
        for d in gdata["games"].values():
            if d["message_id"] == mid:
                await msg.add_reaction(discord.PartialEmoji.from_str(d["emoji"]))
        if is_main:
            await msg.add_reaction(PROPOSE_EMOJI)

    await interaction.followup.send(
        "\u2705 Réactions réordonnées. (Les membres conservent leurs rôles, "
        "mais devront re-réagir s'ils veulent pouvoir les retirer.)",
        ephemeral=True,
    )


@bot.tree.command(description="Liste les jeux configurés.")
async def rr_list(interaction: discord.Interaction):
    gdata = store.guild(interaction.guild_id)
    if not gdata or not gdata["games"]:
        await interaction.response.send_message("Aucun jeu configuré.", ephemeral=True)
        return
    lines = [f"{d['emoji']} {n}" for n, d in sorted(gdata["games"].items())]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(description="Envoie une copie du fichier de config (data.json) — modérateurs.")
async def rr_backup(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message("Réservé aux modérateurs.", ephemeral=True)
        return
    if not os.path.exists(DATA_FILE):
        await interaction.response.send_message("Aucun data.json pour l'instant.", ephemeral=True)
        return
    do_backup()  # profite de la demande pour créer aussi une copie locale
    await interaction.response.send_message(
        "Copie actuelle de la config \U0001F4E6 — garde-la précieusement !",
        file=discord.File(DATA_FILE, filename=f"data-{time.strftime('%Y%m%d')}.json"),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN manquant. Crée un fichier .env (voir .env.example).")
    keep_alive()  # pour Replit, Glitch, etc.
    bot.run(TOKEN)


@bot.event
async def on_message(message: discord.Message):
    # Ignore les messages du bot
    if message.author.bot:
        return

    gdata = store.guild(message.guild.id)
    if gdata:
        # 1. Création de fil automatique sur @everyone
        if message.mention_everyone and message.channel.id in gdata.get("auto_thread_channels", []):
            await message.create_thread(
                name=f"Discussion - {message.author.display_name}", 
                auto_archive_duration=1440 # 24 heures
            )

    # Indispensable si tu ajoutes des commandes basées sur un préfixe (en plus des slash commands)
    await bot.process_commands(message)

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot:
        return
    gdata = store.guild(message.guild.id)
    if gdata and gdata.get("logs_channel_id"):
        log_channel = bot.get_channel(gdata["logs_channel_id"])
        embed = discord.Embed(title="Message supprimé", description=message.content, color=discord.Color.red())
        embed.set_author(name=message.author.name)
        embed.set_footer(text=f"Salon: {message.channel.name}")
        await log_channel.send(embed=embed)

from datetime import datetime

@tasks.loop(hours=24)
async def check_birthdays():
    today = datetime.now().strftime("%d/%m")
    for guild_id, gdata in store.data["guilds"].items():
        if gdata.get("anniv_channel_id"):
            channel = bot.get_channel(gdata["anniv_channel_id"])
            if channel:
                for user_id, date in gdata.get("birthdays", {}).items():
                    if date == today:
                        await channel.send(f"🎉 Joyeux anniversaire <@{user_id}> ! 🎂")

@check_birthdays.before_loop
async def _wait_ready_birthdays():
    await bot.wait_until_ready()

# N'oublie pas d'ajouter check_birthdays.start() dans la fonction on_ready()

@bot.tree.command(description="Ajouter un anniversaire")
async def anniv_add(interaction: discord.Interaction, utilisateur: discord.Member, date: str):
    # Format attendu : JJ/MM
    gdata = store.ensure_guild(interaction.guild_id)
    gdata.setdefault("birthdays", {})[str(utilisateur.id)] = date
    store.save()
    await interaction.response.send_message(f"Anniversaire de {utilisateur.display_name} ajouté le {date}.", ephemeral=True)

@bot.tree.command(description="Configurer le salon des anniversaires")
async def config_anniv(interaction: discord.Interaction, salon: discord.TextChannel):
    gdata = store.ensure_guild(interaction.guild_id)
    gdata["anniv_channel_id"] = salon.id
    store.save()
    await interaction.response.send_message(f"✅ Salon des anniversaires défini sur {salon.mention}.", ephemeral=True)

@bot.tree.command(description="Configurer le salon du Hall of Fame (Starboard)")
async def config_starboard(interaction: discord.Interaction, salon: discord.TextChannel):
    gdata = store.ensure_guild(interaction.guild_id)
    gdata["starboard_channel_id"] = salon.id
    store.save()
    await interaction.response.send_message(f"✅ Hall of Fame défini sur {salon.mention}.", ephemeral=True)

@bot.tree.command(description="Configurer le salon des logs")
async def config_logs(interaction: discord.Interaction, salon: discord.TextChannel):
    gdata = store.ensure_guild(interaction.guild_id)
    gdata["logs_channel_id"] = salon.id
    store.save()
    await interaction.response.send_message(f"✅ Salon des logs défini sur {salon.mention}.", ephemeral=True)

@bot.tree.command(description="Activer les fils automatiques (@everyone) dans un salon")
async def autothread_add(interaction: discord.Interaction, salon: discord.TextChannel):
    gdata = store.ensure_guild(interaction.guild_id)
    channels = gdata.setdefault("auto_thread_channels", [])
    if salon.id not in channels:
        channels.append(salon.id)
        store.save()
    await interaction.response.send_message(f"✅ Création auto de fils activée pour {salon.mention}.", ephemeral=True)