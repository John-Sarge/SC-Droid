import discord
import aiohttp
import json
import xml.etree.ElementTree as ET
from redbot.core import commands, Config
from discord.ext import tasks

class SCDroid(commands.Cog):
    """Advanced Star Citizen integration for API telemetry and fleet management."""

    def __init__(self, bot):
        self.bot = bot
        # Initialize the JSON persistent storage via Red's Config API
        self.config = Config.get_conf(self, identifier=847362948573, force_registration=True)
        
        # Define schemas based on scope
        self.config.register_global(sc_api_key=None, last_comm_link_id=None)
        self.config.register_guild(tracked_channel=None)
        self.config.register_user(fleet=None)
        
        self.session = aiohttp.ClientSession()
        self.rsi_scraper_loop.start()

    def cog_unload(self):
        # Gracefully cancel the background task and close HTTP sockets on unload
        self.rsi_scraper_loop.cancel()
        self.bot.loop.create_task(self.session.close())

    @commands.group(name="sc", invoke_without_command=True)
    async def sc_base(self, ctx):
        """Primary command group for all Star Citizen queries."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @sc_base.command(name="setkey")
    @commands.is_owner()
    async def sc_setkey(self, ctx, key: str):
        """Set your starcitizen-api.com API key (Bot Owner Only)."""
        await self.config.sc_api_key.set(key)
        await ctx.send("Star Citizen API key has been successfully configured.")

    @sc_base.command(name="user")
    async def sc_user(self, ctx, handle: str):
        """Retrieve a Star Citizen user profile."""
        api_key = await self.config.sc_api_key()
        if not api_key:
            return await ctx.send("The API key has not been set by the bot owner yet. Use `[p]sc setkey`.")
            
        # Using the 'auto' endpoint fallback mode to preserve daily live tokens
        url = f"https://api.starcitizen-api.com/{api_key}/v1/auto/user/{handle}"
        
        async with ctx.typing():
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success") == 1:
                            profile = data["data"]["profile"]
                            org = data["data"].get("organization", {})
                            
                            embed = discord.Embed(
                                title=profile.get("display", handle),
                                url=profile.get("page", {}).get("url", ""),
                                color=discord.Color.blue()
                            )
                            embed.set_thumbnail(url=profile.get("image", ""))
                            embed.add_field(name="Handle", value=profile.get("handle", "N/A"))
                            embed.add_field(name="Enlisted", value=profile.get("enlisted", "N/A")[:10])
                            
                            if org:
                                embed.add_field(name="Organization", value=f"{org.get('name')} ({org.get('sid')})", inline=False)
                            
                            await ctx.send(embed=embed)
                        else:
                            await ctx.send("User not found or API returned an error.")
                    else:
                        await ctx.send(f"Upstream API Error: HTTP {response.status}")
            except Exception as e:
                await ctx.send(f"Failed to reach the Star Citizen API: {e}")

    @sc_base.command(name="importfleet")
    async def sc_importfleet(self, ctx):
        """Import your personal fleet from a FleetYards or Hangar XPLORer JSON file."""
        if not ctx.message.attachments:
            return await ctx.send("Please attach your exported JSON file to the command message.")
            
        attachment = ctx.message.attachments
        if not attachment.filename.endswith('.json'):
            return await ctx.send("The attached file must be a.json file.")
            
        try:
            file_bytes = await attachment.read()
            fleet_data = json.loads(file_bytes)
            
            # Simple validation to ensure it's a list format before saving
            if isinstance(fleet_data, list):
                await self.config.user(ctx.author).fleet.set(fleet_data)
                await ctx.send(f"Successfully imported {len(fleet_data)} ships into your personal database!")
            else:
                await ctx.send("Invalid JSON format. Expected a list structure.")
        except json.JSONDecodeError:
            await ctx.send("Failed to parse the JSON file. Ensure the file is not corrupted.")

    @sc_base.command(name="myfleet")
    async def sc_myfleet(self, ctx):
        """View a summary of your imported fleet."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send("Your hangar is empty! Use `[p]sc importfleet` to upload your JSON file.")
            
        # Parse the JSON layout; usually contains 'name', 'type', or 'ship' keys
        ship_names = [ship.get("name") or ship.get("type") or str(ship) for ship in fleet[:10]]
        
        embed = discord.Embed(title=f"{ctx.author.display_name}'s Hangar", color=discord.Color.green())
        embed.description = "\n".join(ship_names)
        
        if len(fleet) > 10:
            embed.set_footer(text=f"...and {len(fleet) - 10} more ships.")
        else:
            embed.set_footer(text=f"Total ships: {len(fleet)}")
            
        await ctx.send(embed=embed)

    @sc_base.command(name="track")
    @commands.has_permissions(manage_channels=True)
    async def sc_track(self, ctx, channel: discord.TextChannel = None):
        """Set the channel for automated RSI Comm-Link updates."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).tracked_channel.set(channel.id)
        await ctx.send(f"RSI website tracking has been enabled. Updates will be posted in {channel.mention}.")

    @tasks.loop(minutes=10.0)
    async def rsi_scraper_loop(self):
        """Periodic background loop for RSI website telemetry extraction."""
        await self.bot.wait_until_ready() # Ensure WebSocket is ready before scraping
        
        # Utilizing community Atom feed for resilient tracking against RSI layout changes
        feed_url = "https://leonick.se/feeds/rsi/atom"
        
        try:
            async with self.session.get(feed_url) as response:
                if response.status!= 200:
                    return
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                
                # XML namespaces required for Atom feeds
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                
                # Extract the most recently published entry
                latest_entry = root.find('atom:entry', ns)
                if latest_entry is None:
                    return
                    
                entry_id = latest_entry.find('atom:id', ns).text
                title = latest_entry.find('atom:title', ns).text
                link = latest_entry.find('atom:link', ns).attrib['href']
                
                last_known_id = await self.config.last_comm_link_id()
                
                # Delta Check: Broadcast if the ID does not match our known cache
                if entry_id!= last_known_id:
                    await self.config.last_comm_link_id.set(entry_id)
                    
                    embed = discord.Embed(
                        title="New RSI Comm-Link",
                        description=f"**{title}**\n({link})",
                        color=discord.Color.gold()
                    )
                    
                    # Iterate over guilds and dispatch
                    all_guilds = await self.config.all_guilds()
                    for guild_id, data in all_guilds.items():
                        channel_id = data.get("tracked_channel")
                        if channel_id:
                            guild = self.bot.get_guild(guild_id)
                            if guild:
                                channel = guild.get_channel(channel_id)
                                if channel:
                                    await channel.send(embed=embed)
        except Exception as e:
            self.bot.logger.error(f"RSI Scraper Loop Exception: {e}")