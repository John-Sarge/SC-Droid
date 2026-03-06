import discord
import aiohttp
import json
import xml.etree.ElementTree as ET
from redbot.core import commands, Config
from discord.ext import tasks

class FleetPaginationView(discord.ui.View):
    def __init__(self, pages, author, timeout=60):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.author = author
        self.current_page = 0
        
        # Initial button state
        # children[0] is Previous (defined first), children[1] is Next (defined second)
        self.children[0].disabled = True
        self.children[1].disabled = len(pages) <= 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("Only the command sender can control this menu.", ephemeral=True)
            return False
        return True

    # Defined FIRST so it appears on the LEFT
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    # Defined SECOND so it appears on the RIGHT
    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    def update_buttons(self):
        self.children[0].disabled = (self.current_page == 0)
        self.children[1].disabled = (self.current_page == len(self.pages) - 1)

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
            
        attachment = ctx.message.attachments[0]
        
        if not attachment.filename.lower().endswith('.json'):
            return await ctx.send("The attached file must be a .json file.")
            
        try:
            file_bytes = await attachment.read()
            fleet_data = json.loads(file_bytes)
            
            # Simple validation to ensure it's a list format before saving
            if isinstance(fleet_data, list):
                await self.config.user(ctx.author).fleet.set(fleet_data)
                
                # Report stats
                count = len(fleet_data)
                manufacturers = set(s.get("manufacturerCode", "Unknown") for s in fleet_data if isinstance(s, dict))
                
                await ctx.send(f"Successfully imported {count} ships from {len(manufacturers)} manufacturers into your personal database!")
            else:
                await ctx.send("Invalid JSON format. Expected a list structure.")
        except json.JSONDecodeError:
            await ctx.send("Failed to parse the JSON file. Ensure the file is not corrupted.")

    @sc_base.group(name="myfleet", invoke_without_command=True)
    async def sc_myfleet(self, ctx):
        """View a summary of your imported fleet."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send("Your hangar is empty! Use `[p]sc importfleet` to upload your JSON file.")
        
        # Calculate ship counts
        total_ships = len(fleet)
        
        # Manufacturer breakdown
        manufacturers = {}
        for ship in fleet:
            man = ship.get("manufacturerName", "Unknown")
            manufacturers[man] = manufacturers.get(man, 0) + 1
            
        sorted_man = sorted(manufacturers.items(), key=lambda x: x[1], reverse=True)
        
        embed = discord.Embed(title=f"{ctx.author.display_name}'s Fleet Summary", color=discord.Color.blue())
        
        embed.description = (
            f"**Total:**\n{total_ships} ships\n\n"
            f"**Manufacturer Focus:**\n" + 
            "\n".join([f"{man}: {count} ships" for man, count in sorted_man[:3]])
        )
        
        embed.set_footer(text="Use `[p]sc myfleet list` to see individual ships.")
        await ctx.send(embed=embed)

    @sc_myfleet.command(name="list")
    async def sc_myfleet_list(self, ctx):
        """List all individual ships in your fleet with pagination."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send("Your hangar is empty! Use `[p]sc importfleet` to upload your JSON file.")
            
        # Sort by name for cleaner display
        sorted_fleet = sorted(fleet, key=lambda x: x.get("name", ""))
        
        pages = []
        chunk_size = 15
        chunks = [sorted_fleet[i:i + chunk_size] for i in range(0, len(sorted_fleet), chunk_size)]
        
        for i, chunk in enumerate(chunks):
            display_lines = []
            for ship in chunk:
                # Handle different JSON formats (FleetYards vs Hangar XPLORer)
                name = ship.get("name") or ship.get("type") or "Unknown Ship"
                custom_name = ship.get("shipName")
                
                if custom_name:
                    display_lines.append(f"**{custom_name}** ({name})")
                else:
                    display_lines.append(name)
            
            embed = discord.Embed(title=f"{ctx.author.display_name}'s Hangar", color=discord.Color.green())
            embed.description = "\n".join(display_lines)
            embed.set_footer(text=f"Page {i+1} of {len(chunks)} | Total ships: {len(sorted_fleet)}")
            pages.append(embed)

        if len(pages) > 0:
            view = FleetPaginationView(pages, ctx.author)
            await ctx.send(embed=pages[0], view=view)
        else:
             await ctx.send("No ships found.")
    
    @sc_base.command(name="find")
    async def sc_find(self, ctx, *, query: str):
        """Search for a ship in your personal fleet."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send("Your hangar is empty! Use `[p]sc importfleet` to upload your JSON file.")
            
        query = query.lower()
        matches = []
        for ship in fleet:
            # Check safely for name, shipName, and manufacturer
            name = (ship.get("name") or "").lower()
            custom_name = (ship.get("shipName") or "").lower()
            manufacturer = (ship.get("manufacturerName") or "").lower()
            
            if query in name or query in custom_name or query in manufacturer:
                matches.append(ship)
        
        if not matches:
            return await ctx.send(f"No ships found matching '{query}'.")
            
        embed = discord.Embed(title=f"Fleet Search: {query}", color=discord.Color.blue())
        
        for ship in matches[:10]:
            name = ship.get("name", "Unknown")
            custom_name = ship.get("shipName")
            manufacturer = ship.get("manufacturerCode", "Unknown")
            slug = ship.get("slug")
            
            display_title = f"{name} - '{custom_name}'" if custom_name else name
            
            details = f"**Manufacturer:** {manufacturer}"
            if slug:
                details += f"\n[View on FleetYards](https://fleetyards.net/ships/{slug})"
            
            embed.add_field(name=display_title, value=details, inline=False)
            
        if len(matches) > 10:
            embed.set_footer(text=f"Showing top 10 of {len(matches)} matches.")
            
        await ctx.send(embed=embed)

    @sc_base.command(name="ship")
    async def sc_ship(self, ctx, *, ship_name: str):
        """Search for a ship on FleetYards and display its statistics."""
        url = "https://api.fleetyards.net/v1/models"
        params = {"name": ship_name}
        
        async with ctx.typing():
            try:
                async with self.session.get(url, params=params) as response:
                    if response.status != 200:
                        return await ctx.send(f"FleetYards API returned an error: {response.status}")
                    
                    data = await response.json()
                    # FleetYards returns a list or direct object depending on endpoint, usually list for search
                    if not data:
                        return await ctx.send(f"No ships found matching '{ship_name}'.")
                    
                    # Exact match or first result
                    ship = data[0] if isinstance(data, list) else data
                    
                    embed = discord.Embed(
                        title=f"{ship.get('name', 'Unknown')} ({ship.get('manufacturer', {}).get('code', 'UNK')})",
                        url=f"https://fleetyards.net/ships/{ship.get('slug')}",
                        color=discord.Color.dark_red()
                    )
                    
                    if ship.get("storeImage"):
                        embed.set_image(url=ship["storeImage"])
                    elif ship.get("image"):
                        embed.set_image(url=ship["image"])
                        
                    manufacturer = ship.get("manufacturer", {}).get("name", "Unknown")
                    embed.add_field(name="Manufacturer", value=manufacturer, inline=True)
                    embed.add_field(name="Focus", value=ship.get("focus", "N/A"), inline=True)
                    embed.add_field(name="Class", value=ship.get("classification", "N/A"), inline=True)
                    
                    # Stats
                    stats = []
                    if ship.get("price"): stats.append(f"Price: ${ship['price']}")
                    if ship.get("maxCrew"): stats.append(f"Max Crew: {ship['maxCrew']}")
                    if ship.get("cargo"): stats.append(f"Cargo: {ship['cargo']} SCU")
                    if ship.get("scmSpeed"): stats.append(f"SCM Speed: {ship['scmSpeed']} m/s")
                    if ship.get("afterburnerSpeed"): stats.append(f"Max Speed: {ship['afterburnerSpeed']} m/s")
                    
                    embed.add_field(name="Specifications", value="\n".join(stats) or "No stats available", inline=False)
                    embed.add_field(name="Status", value=ship.get("productionStatus", "Unknown"), inline=True)
                    
                    await ctx.send(embed=embed)
                    
            except Exception as e:
                await ctx.send(f"Failed to query FleetYards: {e}")

    @sc_base.command(name="org")
    async def sc_org(self, ctx, symbol: str):
        """Retrieve a Star Citizen Organization profile."""
        api_key = await self.config.sc_api_key()
        if not api_key:
            return await ctx.send("The API key has not been set by the bot owner yet. Use `[p]sc setkey`.")
            
        url = f"https://api.starcitizen-api.com/{api_key}/v1/auto/organization/{symbol}"
        
        async with ctx.typing():
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success") == 1:
                            org = data["data"]
                            
                            embed = discord.Embed(
                                title=f"{org.get('name')} [{org.get('sid')}]",
                                url=org.get("url", ""),
                                description=org.get("headline", ""),
                                color=discord.Color.blurple()
                            )
                            
                            if org.get("logo"):
                                embed.set_thumbnail(url=org["logo"])
                            
                            if org.get("banner"):
                                embed.set_image(url=org["banner"])
                                
                            embed.add_field(name="Archetype", value=org.get("archetype", "N/A"), inline=True)
                            embed.add_field(name="Members", value=str(org.get("members", "N/A")), inline=True)
                            embed.add_field(name="Primary Language", value=org.get("lang", "N/A"), inline=True)
                            
                            # Focus
                            focus = [org.get("primaryActivity"), org.get("secondaryActivity")]
                            focus = [f for f in focus if f]
                            if focus:
                                embed.add_field(name="Focus", value=", ".join(focus), inline=False)
                                
                            await ctx.send(embed=embed)
                        else:
                            await ctx.send("Organization not found or API returned an error.")
                    else:
                        await ctx.send(f"Upstream API Error: HTTP {response.status}")
            except Exception as e:
                await ctx.send(f"Failed to reach the Star Citizen API: {e}")

    @sc_base.command(name="addship")
    async def sc_addship(self, ctx, *, ship_name: str):
        """Add a ship to your personal fleet by searching FleetYards."""
        # Search FleetYards first to get standardized data
        url = "https://api.fleetyards.net/v1/models"
        params = {"name": ship_name}
        
        async with ctx.typing():
            try:
                async with self.session.get(url, params=params) as response:
                    if response.status != 200:
                        return await ctx.send("Could not verify ship with FleetYards.")
                        
                    data = await response.json()
                    if not data:
                        return await ctx.send(f"No ships found matching '{ship_name}'.")
                    
                    found_ship = data[0] if isinstance(data, list) else data
                    
                    # Construct the ship object to match the schema used by importfleet
                    new_ship = {
                        "name": found_ship.get("name"),
                        "manufacturerName": found_ship.get("manufacturer", {}).get("name", "Unknown"),
                        "manufacturerCode": found_ship.get("manufacturer", {}).get("code", "UNK"),
                        "slug": found_ship.get("slug"),
                        "shipName": None
                    }
                    
                    fleet = await self.config.user(ctx.author).fleet()
                    if fleet is None:
                        fleet = []
                    
                    # Check for duplicates (optional, but good practice)
                    # We'll allow duplicates if they want multiple of same ship, 
                    # but maybe warn? For now, just add.
                    fleet.append(new_ship)
                    
                    await self.config.user(ctx.author).fleet.set(fleet)
                    await ctx.send(f"Added **{new_ship['name']}** to your fleet.")
                    
            except Exception as e:
                await ctx.send(f"Error adding ship: {e}")

    @sc_base.command(name="removeship")
    async def sc_removeship(self, ctx, *, ship_name: str):
        """Remove a ship from your personal fleet."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send("Your hangar is empty.")
            
        found = False
        new_fleet = []
        ship_name_lower = ship_name.lower()
        
        for ship in fleet:
            if not found:
                name = (ship.get("name") or "").lower()
                custom = (ship.get("shipName") or "").lower()
                
                # Try to fuzzy match or exact match
                if ship_name_lower == name or ship_name_lower == custom or ship_name_lower in name:
                    found = True
                    continue # Remove this one
            
            new_fleet.append(ship)
            
        if found:
            await self.config.user(ctx.author).fleet.set(new_fleet)
            await ctx.send(f"Removed **{ship_name}** from your fleet.")
        else:
            await ctx.send(f"Could not find a ship named '{ship_name}' in your fleet.")

    @sc_base.command(name="status")
    async def sc_status(self, ctx):
        """Check the current status of the Persistent Universe."""
        url = "https://status.robertsspaceindustries.com/api/5.0/incidents.json"
        
        async with ctx.typing():
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        incidents = data.get("incidents", [])
                        
                        if not incidents:
                            await ctx.send("No active incidents reported. All systems operational.")
                            return
                        
                        embed = discord.Embed(title="RSI Platform Status", url="https://status.robertsspaceindustries.com/", color=discord.Color.orange())
                        
                        for incident in incidents[:3]: # Show top 3 recent incidents
                            title = incident.get("title", "Unknown Incident")
                            impact = incident.get("impact", "Unknown")
                            status = incident.get("status", "Unknown")
                            id = incident.get("id")
                            
                            value_text = f"**Impact:** {impact}\n**Status:** {status}"
                            if id:
                                value_text += f"\n[More Info](https://status.robertsspaceindustries.com/incidents/{id})"
                            
                            embed.add_field(
                                name=title,
                                value=value_text,
                                inline=False
                            )
                        
                        if len(incidents) > 3:
                            embed.set_footer(text=f"And {len(incidents) - 3} more active incidents.")
                            
                        await ctx.send(embed=embed)
                    else:
                        await ctx.send("Could not retrieve status from RSI.")
            except Exception as e:
                await ctx.send(f"Failed to reach RSI Status Page: {e}")

    @sc_base.command(name="news")
    async def sc_news(self, ctx):
        """Manually fetch the latest RSI Comm-Link post."""
        feed_url = "https://leonick.se/feeds/rsi/atom"
        
        async with ctx.typing():
            try:
                async with self.session.get(feed_url) as response:
                    if response.status != 200:
                        return await ctx.send("Could not fetch RSI news feed.")
                    
                    xml_data = await response.text()
                    root = ET.fromstring(xml_data)
                    ns = {'atom': 'http://www.w3.org/2005/Atom'}
                    
                    latest_entry = root.find('atom:entry', ns)
                    if latest_entry is None:
                        return await ctx.send("No news found.")
                        
                    title = latest_entry.find('atom:title', ns).text
                    link = latest_entry.find('atom:link', ns).attrib['href']
                    updated = latest_entry.find('atom:updated', ns).text
                    
                    embed = discord.Embed(
                        title="Latest RSI Comm-Link",
                        description=f"**[{title}]({link})**",
                        color=discord.Color.gold()
                    )
                    embed.set_footer(text=f"Published: {updated}")
                    
                    await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send(f"Error fetching news: {e}")

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
