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
    # Allows the user to move to the previous page of the fleet list
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    # Defined SECOND so it appears on the RIGHT
    # Allows the user to move to the next page of the fleet list
    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(len(self.pages) - 1, self.current_page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    def update_buttons(self):
        self.children[0].disabled = (self.current_page == 0)
        self.children[1].disabled = (self.current_page == len(self.pages) - 1)

    async def on_timeout(self):
        # When the view times out (default 60s), remove the message to reduce clutter
        try:
            # We need the message object to delete it. 
            # In discord.py, if the view was sent with a message, we can't always access it directly 
            # unless we stored it. 
            # However, since we are using this in a command, best practice is to store the message context.
            if hasattr(self, 'message') and self.message:
                await self.message.delete()
        except:
            pass # Message might already be deleted

class ShipSelectView(discord.ui.View):
    # View containing a dropdown menu for selecting a specific ship from multiple search results
    def __init__(self, ships, author, timeout=60):
        super().__init__(timeout=timeout)
        self.ships = ships
        self.author = author
        self.selected_ship = None
        
        # Add Select Menu
        options = []
        for ship in ships[:25]: # Select menus have a 25 option limit, showing top matches
            label = f"{ship.get('name')} ({ship.get('manufacturer', {}).get('code', 'UNK')})"
            slug = ship.get('slug')
            # Fallback for value if slug is missing
            value = slug if slug else ship.get('name')
            options.append(discord.SelectOption(label=label, value=value))
            
        self.add_item(ShipSelectCallback(options))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Prevents other users from interfering with the ship selection menu
        if interaction.user != self.author:
            await interaction.response.send_message("Only the command sender can select a ship.", ephemeral=True)
            return False
        return True

class ShipSelectCallback(discord.ui.Select):
    # Callback handler for when a user picks an option in the ShipSelectView dropdown
    def __init__(self, options):
        super().__init__(placeholder="Select a ship...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # Access the View via self.view to store the selection result
        self.view.selected_ship = self.values[0]
        self.view.stop()
        await interaction.response.defer() # Acknowledge without sending message yet

class SCDroid(commands.Cog):
    """Advanced Star Citizen integration for API telemetry and fleet management."""

    def __init__(self, bot):
        self.bot = bot
        # Initialize the JSON persistent storage via Red's Config API
        self.config = Config.get_conf(self, identifier=847362948573, force_registration=True)
        
        # Define schemas based on scope
        self.config.register_global(sc_api_key=None, last_comm_link_id=None, last_roadmap_update=None)
        self.config.register_guild(tracked_channel=None)
        self.config.register_user(fleet=None)
        
        self.session = aiohttp.ClientSession()
        self.ship_cache = [] # Cache to store all ships locally
        self.bot.loop.create_task(self.update_ship_cache())
        self.rsi_scraper_loop.start() # Starts the background task for news/status updates
        self.roadmap_scraper_loop.start() # Starts the background task for roadmap tracking

    async def update_ship_cache(self):
        """Fetch and cache the entire ship list from FleetYards to enable fast searching and comparisons."""
        url = "https://api.fleetyards.net/v1/models"
        params = {"perPage": 200, "page": 1}
        all_ships = []
        
        try:
            while True:
                async with self.session.get(url, params=params) as response:
                    if response.status != 200:
                        self.bot.logger.error(f"SCDroid: FleetYards API returned {response.status}")
                        break
                    
                    data = await response.json()
                    
                    # Safety check: API might return empty list or different format
                    if not data or not isinstance(data, list):
                        break
                        
                    all_ships.extend(data)
                    
                    # If we received fewer items than requested, we've reached the last page
                    if len(data) < params["perPage"]:
                        break
                        
                    params["page"] += 1
            
            if all_ships:
                self.ship_cache = all_ships
                self.bot.logger.info(f"SCDroid: Cached {len(self.ship_cache)} ships from FleetYards.")
            else:
                self.bot.logger.warning("SCDroid: No ships retrieved from FleetYards.")
                
        except Exception as e:
            self.bot.logger.error(f"Failed to update ship cache: {e}")

    def cog_unload(self):
        # Gracefully cancel the background task and close HTTP sockets on unload
        self.rsi_scraper_loop.cancel()
        self.roadmap_scraper_loop.cancel()
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
            return await ctx.send(f"The API key has not been set by the bot owner yet. Use `{ctx.clean_prefix}sc setkey`.")
            
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
        """View a summary of your imported fleet, including manufacturer stats."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send(f"Your hangar is empty! Use `{ctx.clean_prefix}sc importfleet` to upload your JSON file.")
        
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
        
        embed.set_footer(text=f"Use `{ctx.clean_prefix}sc myfleet list` to see individual ships.")
        await ctx.send(embed=embed)

    @sc_myfleet.command(name="list")
    async def sc_myfleet_list(self, ctx):
        """List all individual ships in your fleet with pagination."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send(f"Your hangar is empty! Use `{ctx.clean_prefix}sc importfleet` to upload your JSON file.")
            
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
            view = FleetPaginationView(pages, ctx.author, timeout=60) # 60 seconds timeout
            msg = await ctx.send(embed=pages[0], view=view)
            view.message = msg # Store message so on_timeout can delete it
        else:
             await ctx.send("No ships found.")
    
    @sc_base.command(name="find")
    async def sc_find(self, ctx, *, query: str):
        """Search for a ship in your personal fleet."""
        fleet = await self.config.user(ctx.author).fleet()
        if not fleet:
            return await ctx.send(f"Your hangar is empty! Use `{ctx.clean_prefix}sc importfleet` to upload your JSON file.")
            
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
        """Search for a ship (locally cached) and display its statistics."""
        if not self.ship_cache:
            await ctx.send("Ship cache is still building... please wait a moment.")
            await self.update_ship_cache()
            
        params = ship_name.lower().split()
        matches = []
        
        # Simple fuzzy search against local cache
        for ship in self.ship_cache:
            name = (ship.get("name") or "").lower()
            manufacturer = (ship.get("manufacturer", {}).get("name") or "").lower()
            code = (ship.get("manufacturer", {}).get("code") or "").lower()
            
            # Match if ALL words in query are present in name/manufacturer
            if all(word in name for word in params) or all(word in manufacturer for word in params) or ship_name.lower() == name:
                matches.append(ship)
        
        if not matches:
            return await ctx.send(f"No ships found matching '{ship_name}'.")

        # Sort matches by exact name match first, then by string length
        matches.sort(key=lambda x: (x.get("name", "").lower() != ship_name.lower(), len(x.get("name", ""))))
        
        selected_ship = None
        
        if len(matches) > 1:
            view = ShipSelectView(matches, ctx.author)
            msg = await ctx.send("Multiple ships found. Please select one:", view=view)
            
            if await view.wait():
                return await ctx.send("Selection timed out.")
            
            selected_slug = view.selected_ship
            selected_ship = next((s for s in self.ship_cache if s.get("slug") == selected_slug or s.get("name") == selected_slug), None)
            try:
                await msg.delete()
            except:
                pass
        else:
            selected_ship = matches[0]

        if not selected_ship:
             return await ctx.send("Error retrieving ship details.")

        embed = discord.Embed(
            title=f"{selected_ship.get('name', 'Unknown')} ({selected_ship.get('manufacturer', {}).get('code', 'UNK')})",
            url=f"https://fleetyards.net/ships/{selected_ship.get('slug')}",
            color=discord.Color.dark_red()
        )
        
        if selected_ship.get("storeImage"):
            embed.set_image(url=selected_ship["storeImage"])
        elif selected_ship.get("image"):
            embed.set_image(url=selected_ship["image"])
            
        manufacturer = selected_ship.get("manufacturer", {}).get("name", "Unknown")
        embed.add_field(name="Manufacturer", value=manufacturer, inline=True)
        embed.add_field(name="Focus", value=selected_ship.get("focus", "N/A"), inline=True)
        embed.add_field(name="Class", value=selected_ship.get("classification", "N/A"), inline=True)
        
        stats = []
        if selected_ship.get("price"): stats.append(f"Price: ${selected_ship['price']}")
        if selected_ship.get("maxCrew"): stats.append(f"Max Crew: {selected_ship['maxCrew']}")
        if selected_ship.get("cargo"): stats.append(f"Cargo: {selected_ship['cargo']} SCU")
        if selected_ship.get("scmSpeed"): stats.append(f"SCM Speed: {selected_ship['scmSpeed']} m/s")
        if selected_ship.get("afterburnerSpeed"): stats.append(f"Max Speed: {selected_ship['afterburnerSpeed']} m/s")
        
        embed.add_field(name="Specifications", value="\n".join(stats) or "No stats available", inline=False)
        embed.add_field(name="Status", value=selected_ship.get("productionStatus", "Unknown"), inline=True)
        
        await ctx.send(embed=embed)

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
        if not self.ship_cache:
            await ctx.send("Ship cache is still building... please wait a moment.")
            await self.update_ship_cache()
            
        params = ship_name.lower().split()
        matches = []
        
        # Simple fuzzy search
        for ship in self.ship_cache:
            name = (ship.get("name") or "").lower()
            if all(word in name for word in params) or ship_name.lower() == name:
                matches.append(ship)
        
        if not matches:
            return await ctx.send(f"No ships found matching '{ship_name}'.")

        # Sort matches
        matches.sort(key=lambda x: (x.get("name", "").lower() != ship_name.lower(), len(x.get("name", ""))))
        
        selected_ship = None
        
        if len(matches) > 1:
            view = ShipSelectView(matches, ctx.author)
            msg = await ctx.send("Multiple ships found. Please select one:", view=view)
            
            if await view.wait():
                return await ctx.send("Selection timed out.")
                
            selected_slug = view.selected_ship
            selected_ship = next((s for s in self.ship_cache if s.get("slug") == selected_slug or s.get("name") == selected_slug), None)
            try:
                await msg.delete()
            except:
                pass
        else:
            selected_ship = matches[0]

        if not selected_ship:
             return await ctx.send("Cancelled.")
        
        new_ship = {
            "name": selected_ship.get("name"),
            "manufacturerName": selected_ship.get("manufacturer", {}).get("name", "Unknown"),
            "manufacturerCode": selected_ship.get("manufacturer", {}).get("code", "UNK"),
            "slug": selected_ship.get("slug"),
            "shipName": None
        }
        
        fleet = await self.config.user(ctx.author).fleet()
        if fleet is None:
            fleet = []
        
        fleet.append(new_ship)
        
        await self.config.user(ctx.author).fleet.set(fleet)
        await ctx.send(f"Added **{new_ship['name']}** to your fleet.")

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
        """Check the current status of the Persistent Universe by scraping the RSI Status Page."""
        # Using the main status page HTML since the API endpoint (api/5.0/incidents.json)
        # is now protected by 403 Forbidden (Likely Cloudflare WAF protection)
        url = "https://status.robertsspaceindustries.com/"
        
        async with ctx.typing():
            try:
                # Set a User-Agent to mimic a browser to bypass basic anti-bot checks
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                }
                async with self.session.get(url, headers=headers) as response:
                    # If direct simple request fails, fail gracefully
                    if response.status != 200:
                        return await ctx.send(f"Could not retrieve status from RSI (HTTP {response.status}).")
                    
                    html_content = await response.text()
                    
                    # Simple parsing to check for the global status indicator
                    # The HTML contains <div class="summary" data-status="operational">
                    
                    status_text = "Unknown"
                    color = discord.Color.greyple()
                    
                    if 'data-status="operational"' in html_content:
                        status_text = "Operational"
                        color = discord.Color.green()
                    elif 'data-status="maintenance"' in html_content:
                        status_text = "Maintenance"
                        color = discord.Color.orange()
                    elif 'data-status="degraded"' in html_content:
                        status_text = "Degraded Performance"
                        color = discord.Color.gold()
                    elif 'data-status="major"' in html_content:
                        status_text = "Major Outage"
                        color = discord.Color.red()
                        
                    embed = discord.Embed(
                        title="RSI Platform Status", 
                        url=url, 
                        color=color,
                        description=f"**Current Global Status:** {status_text}"
                    )
                    
                    # Attempt to extract recent incidents title if possible
                    # This is brittle with regex but better than nothing without BeautifulSoup
                    # <div class="issue__header ">\n<h3>\nLive Services Disruption\n</h3>
                    try:
                        import re
                        # Find the first issue header 
                        match = re.search(r'<div class="issue__header ">\s*<h3>\s*(.*?)\s*</h3>', html_content, re.DOTALL)
                        if match:
                            latest_incident = match.group(1).strip()
                            embed.add_field(name="Latest Incident", value=latest_incident, inline=False)
                    except:
                        pass
                        
                    await ctx.send(embed=embed)

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

    @sc_base.command(name="reloadships")
    @commands.is_owner()
    async def sc_reloadships(self, ctx):
        """Force a manual refresh of the ship database from FleetYards."""
        await ctx.send("Manually refreshing ship database...")
        await self.update_ship_cache()
        await ctx.send(f"Done. Cache now contains {len(self.ship_cache)} ships.")

    @sc_base.command(name="track")
    @commands.has_permissions(manage_channels=True)
    async def sc_track(self, ctx, channel: discord.TextChannel = None):
        """Set the channel for automated RSI Comm-Link updates."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).tracked_channel.set(channel.id)
        await ctx.send(f"RSI website tracking has been enabled. Updates will be posted in {channel.mention}.")

    @tasks.loop(minutes=10.0)
    async def rsi_scraper_loop(self):
        """Periodic background loop checking RSI's Atom feed for new Comm-Links."""
        await self.bot.wait_until_ready() # Ensure WebSocket is ready before scraping
        
        # Utilizing community Atom feed (leonick.se) for resilient tracking against RSI layout changes
        feed_url = "https://leonick.se/feeds/rsi/atom"
        
        try:
            async with self.session.get(feed_url) as response:
                if response.status != 200:
                    return
                
                xml_data = await response.text()
                root = ET.fromstring(xml_data)
                
                # XML namespaces required for Atom feeds
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                
                # Extract the most recently published entry
                latest_entry = root.find('atom:entry', ns)
                if latest_entry is None:
                    return
                
                # Extract relevant fields
                entry_id = latest_entry.find('atom:id', ns).text
                title = latest_entry.find('atom:title', ns).text
                link = latest_entry.find('atom:link', ns).attrib['href']
                
                # Get last known state
                last_known_id = await self.config.last_comm_link_id()
                
                # Delta Check: Broadcast if the ID does not match our known cache
                if entry_id != last_known_id:
                    await self.config.last_comm_link_id.set(entry_id)
                    
                    embed = discord.Embed(
                        title="New RSI Comm-Link",
                        description=f"**[{title}]({link})**",
                        color=discord.Color.gold()
                    )
                    
                    # Dispatch using helper
                    await self.dispatch_to_tracked_channels(embed)
                    
        except Exception as e:
            self.bot.logger.error(f"RSI Scraper Loop Exception: {e}")

    @tasks.loop(minutes=60.0) # Check roadmap less frequently
    async def roadmap_scraper_loop(self):
        """Periodic check for Roadmap updates."""
        await self.bot.wait_until_ready()
        
        # This endpoint returns the last updated date for the roadmap
        # Note: RSI might change this endpoint structure. 
        # API v1 board 1 is generally the Progress Tracker board.
        url = "https://robertsspaceindustries.com/api/roadmap/v1/boards/1"
        
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    return
            
                data = await response.json()
                if not data or not data.get("success") == 1:
                    return
                
                # The 'modified' field contains the timestamp of the last edit
                # data structure: { success: 1, data: { modified: "YYYY-MM-DD HH:MM:SS", ... } }
                board_data = data.get("data")
                if not board_data:
                    return

                last_modified = board_data.get("modified")
                
                last_known_mod = await self.config.last_roadmap_update()
                
                # Only announce if we have a previous value to compare to (avoid spam on first run)
                # Or set it if it's new
                if not last_known_mod:
                     await self.config.last_roadmap_update.set(last_modified)
                elif last_modified and last_modified != last_known_mod:
                    await self.config.last_roadmap_update.set(last_modified)
                    
                    embed = discord.Embed(
                        title="Star Citizen Roadmap Updated",
                        description="The Progress Tracker has been updated!\n\n[View Roadmap](https://robertsspaceindustries.com/roadmap/progress-tracker)",
                        color=discord.Color.blue()
                    )
                    # Using a generic roadmap image or trying to parse one would be good here
                    # embed.set_image(url="...") 
                    embed.set_footer(text=f"Updated: {last_modified}")
                    
                    await self.dispatch_to_tracked_channels(embed)
                    
        except Exception as e:
            self.bot.logger.error(f"Roadmap Scraper Loop Exception: {e}")

    async def dispatch_to_tracked_channels(self, embed):
        """Helper to send updates to all tracked channels."""
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            channel_id = data.get("tracked_channel")
            if channel_id:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(embed=embed)
                        except discord.Forbidden:
                            pass # Bot lost permissions

    @sc_base.command(name="compare")
    async def sc_compare(self, ctx, *, query: str):
        """Compare two ships side-by-side. Usage: `[p]sc compare <ship1> vs <ship2>`"""
        if " vs " not in query.lower():
             return await ctx.send(f"Please separate ship names with ' vs '. Example: `{ctx.clean_prefix}sc compare titan vs cutlass`")
        
        ship1_query, ship2_query = query.split(" vs " if " vs " in query else " VS ", 1)
        
        # Helper to find/select a ship
        async def get_ship(query):
            params = query.lower().split()
            matches = []
            for ship in self.ship_cache:
                name = (ship.get("name") or "").lower()
                manufacturer = (ship.get("manufacturer", {}).get("name") or "").lower()
                
                # Match logic
                if all(word in name for word in params) or \
                   all(word in manufacturer for word in params) or \
                   query.lower() == name:
                    matches.append(ship)
            
            if not matches:
                await ctx.send(f"No ships found matching '{query}'.")
                return None
                
            matches.sort(key=lambda x: (x.get("name", "").lower() != query.lower(), len(x.get("name", ""))))
            
            if len(matches) > 1:
                view = ShipSelectView(matches, ctx.author)
                msg = await ctx.send(f"Multiple ships found for '**{query}**'. Please select one:", view=view)
                
                if await view.wait():
                    await ctx.send("Selection timed out.")
                    return None
                
                selected_slug = view.selected_ship
                selected_ship = next((s for s in self.ship_cache if s.get("slug") == selected_slug or s.get("name") == selected_slug), None)
                try:
                    await msg.delete()
                except:
                    pass
                return selected_ship
            else:
                return matches[0]

        ship1 = await get_ship(ship1_query.strip())
        if not ship1: return
        
        ship2 = await get_ship(ship2_query.strip())
        if not ship2: return
            
        # Build Comparison Embed
        embed = discord.Embed(
            title=f"Compare: {ship1['name']} vs {ship2['name']}",
            color=discord.Color.magenta()
        )
        
        # Format: Field Name | Ship 1 Value | Ship 2 Value
        # Updates Logic: Lower values for Price, Crew, Mass, Length are "better" (reverse=True)
        def compare_val(field, label, suffix="", reverse=False):
            v1 = ship1.get(field)
            v2 = ship2.get(field)
            
            val1_str = "N/A"
            val2_str = "N/A"

            # Parse numbers
            n1 = None
            n2 = None
            
            if v1 is not None:
                try:
                    n1 = float(str(v1).replace('$', '').replace(',', ''))
                    # Format number nicely
                    if n1.is_integer():
                         val1_str = f"{int(n1):,}"
                    else:
                         val1_str = f"{n1:,.2f}"
                except:
                    val1_str = str(v1)

            if v2 is not None:
                try:
                    n2 = float(str(v2).replace('$', '').replace(',', ''))
                     # Format number nicely
                    if n2.is_integer():
                         val2_str = f"{int(n2):,}"
                    else:
                         val2_str = f"{n2:,.2f}"
                except:
                     val2_str = str(v2)

            # Compare if both are numbers
            if n1 is not None and n2 is not None:
                if n1 != n2:
                    # Determine winner
                    # If reverse is True (e.g. Price, Mass), lower is better
                    v1_better = False
                    if reverse:
                         if n1 < n2: v1_better = True
                    else:
                         if n1 > n2: v1_better = True
                    
                    if v1_better:
                        val1_str = f"**{val1_str}** 🔼"
                    else:
                        val2_str = f"**{val2_str}** 🔼"

            embed.add_field(name=f"{label} (1)", value=f"{val1_str}{suffix}", inline=True)
            embed.add_field(name=f"{label} (2)", value=f"{val2_str}{suffix}", inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        embed.add_field(name="Ship 1", value=ship1['name'], inline=True)
        embed.add_field(name="Ship 2", value=ship2['name'], inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        compare_val("price", "Price", " UEC", reverse=True) # Lower price is better
        compare_val("scmSpeed", "SCM Speed", " m/s")
        compare_val("maxCrew", "Max Crew", reverse=True)
        compare_val("cargo", "Cargo", " SCU")
        compare_val("length", "Length", " m", reverse=True)
        compare_val("mass", "Mass", " kg", reverse=True) 
        
        await ctx.send(embed=embed)
