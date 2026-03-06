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

    async def update_view(self, interaction: discord.Interaction):
        # Disable Prev if on page 0, disable Next if on last page
        self.children[0].disabled = (self.current_page == 0)
        self.children[1].disabled = (self.current_page == len(self.pages) - 1)
        
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

class ShipSelectionView(discord.ui.View):
    def __init__(self, matches, author, bot, original_ctx):
        super().__init__(timeout=60)
        self.matches = matches
        self.author = author
        self.bot = bot
        self.original_ctx = original_ctx
        
        # Create select menu options (max 25)
        options = []
        for i, ship in enumerate(matches[:25]):
            options.append(discord.SelectOption(
                label=ship.get("name", "Unknown")[:100],
                description=ship.get("slug", "")[:100],
                value=str(i)
            ))

        self.select_menu = discord.ui.Select(
            placeholder="Select a ship...",
            min_values=1,
            max_values=1,
            options=options
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        # Disable the view so they can't click again
        self.stop() 
        selected_index = int(self.select_menu.values[0])
        selected_ship = self.matches[selected_index]
        
        # Call a helper method on the cog instance if possible, or just build the embed here.
        # Since I don't have easy access to 'self' (the cog), I will replicate the embed building logic here 
        # or better yet, inject the build function.
        # Actually, let's just trigger a new task or modify the message directly.
        
        # We need to construct the embed for the selected ship.
        embed = self.build_ship_embed(selected_ship)
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    def build_ship_embed(self, ship):
        s_name = ship.get("name", "Unknown Ship")
        s_desc = ship.get("description", "No description available.")
        s_focus = ship.get("focus", "N/A")
        s_size = ship.get("sizeLabel", "N/A")
        
        min_crew = ship.get("minCrew", "?")
        max_crew = ship.get("maxCrew", "?")
        s_crew = f"{min_crew} - {max_crew}" if min_crew != max_crew else str(min_crew)
        
        s_price = ship.get("priceLabel", "Not available in-game")
        s_pledge = ship.get("pledgePriceLabel", "N/A")
        
        media = ship.get("media", {})
        store_img = media.get("storeImage")
        
        img_main = None
        if isinstance(store_img, dict):
            img_main = store_img.get("large") or store_img.get("medium") or store_img.get("source")
        elif isinstance(store_img, str):
            img_main = store_img

        embed = discord.Embed(title=s_name, description=s_desc[:2048], color=discord.Color.dark_red())
        if img_main:
            embed.set_image(url=img_main)
        
        manufacturer = ship.get("manufacturer", {}).get("name", "Unknown")
        embed.add_field(name="Manufacturer", value=manufacturer, inline=True)
        embed.add_field(name="Focus", value=s_focus, inline=True)
        embed.add_field(name="Size", value=s_size, inline=True)
        embed.add_field(name="Crew", value=s_crew, inline=True)
        embed.add_field(name="Price (aUEC)", value=str(s_price), inline=True)
        embed.add_field(name="Pledge", value=str(s_pledge), inline=True)
        
        slug = ship.get("slug")
        if slug:
            embed.add_field(name="FleetYards Link", value=f"https://fleetyards.net/ships/{slug}", inline=False)
            
        return embed

class SCDroid(commands.Cog):
    """Advanced Star Citizen integration for API telemetry and fleet management."""

    def __init__(self, bot):
        self.bot = bot
        # Initialize the JSON persistent storage via Red's Config API
        self.config = Config.get_conf(self, identifier=847362948573, force_registration=True)
        
        # Define schemas based on scope
        self.config.register_global(
            sc_api_key=None, 
            last_comm_link_id=None,
            last_roadmap_id=None,
            last_dev_id=None
        )
        self.config.register_guild(
            tracked_channel=None,
            track_roadmap=False,
            track_devtracker=False
        )
        
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
                                if org.get('image'):
                                    embed.set_footer(text=f"Member of {org.get('name')}", icon_url=org.get('image'))
                            
                            await ctx.send(embed=embed)
                        else:
                            await ctx.send("User not found or API returned an error.")
                    else:
                        await ctx.send(f"Upstream API Error: HTTP {response.status}")
            except Exception as e:
                await ctx.send(f"Failed to reach the Star Citizen API: {e}")

    @sc_base.command(name="org")
    async def sc_org(self, ctx, sid: str):
        """Retrieve a Star Citizen Organization profile."""
        api_key = await self.config.sc_api_key()
        if not api_key:
            return await ctx.send("The API key has not been set by the bot owner yet. Use `[p]sc setkey`.")
            
        # Ensure SID is uppercase as the API is strictly case-sensitive for IDs
        sid = sid.upper()
        url = f"https://api.starcitizen-api.com/{api_key}/v1/auto/organization/{sid}"
        
        async with ctx.typing():
            try:
                async with self.session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("success") == 1:
                            org = data.get("data")
                            
                            if not org:
                                return await ctx.send(f"Organization '{sid}' data is missing/empty.")

                            # Handle complex headline object (html/plaintext)
                            headline = org.get('headline', '')
                            if isinstance(headline, dict):
                                headline = headline.get('plaintext', '')
                            
                            embed = discord.Embed(
                                title=f"{org.get('name')} [{org.get('sid')}]",
                                url=org.get('url', ''),
                                description=headline[:2000] if headline else '',
                                color=discord.Color.brand_red()
                            )
                            embed.set_thumbnail(url=org.get('logo', ''))
                            embed.set_image(url=org.get('banner', ''))
                            
                            embed.add_field(name="Archetype", value=org.get('archetype', 'N/A'), inline=True)
                            embed.add_field(name="Members", value=str(org.get('members', 'N/A')), inline=True)
                            embed.add_field(name="Language", value=org.get('lang', 'N/A'), inline=True)
                            
                            if org.get('recruiting'):
                                embed.add_field(name="Recruiting", value="Active", inline=True)
                            else:
                                embed.add_field(name="Recruiting", value="Closed", inline=True)

                            await ctx.send(embed=embed)
                        else:
                            await ctx.send(f"Organization '{sid}' not found.")
                    else:
                        await ctx.send(f"Upstream API Error: HTTP {response.status}")
            except Exception as e:
                await ctx.send(f"Failed to reach the Star Citizen API: {e}")

    @sc_base.command(name="ship")
    async def sc_ship(self, ctx, *, ship_name: str):
        """Lookup ship statistics from the global FleetYards database."""
        ship_name = ship_name.strip()
        url = "https://api.fleetyards.net/v1/models"
        
        async with ctx.typing():
            try:
                # The FleetYards API partial search is unreliable for non-exact slugs.
                # We will fetch all models (pagination required) and filter locally.
                # Currently ~230 models, so 2 pages of 200 covers it effectively.
                
                combined_data = []
                async with self.session.get(url, params={"page": 1, "perPage": 200}) as r1:
                    if r1.status == 200:
                        combined_data.extend(await r1.json())
                
                # Fetch page 2 just to be safe
                async with self.session.get(url, params={"page": 2, "perPage": 200}) as r2:
                     if r2.status == 200:
                        combined_data.extend(await r2.json())

                if not combined_data:
                    return await ctx.send("FleetYards API returned no data. Please try again later.")
                
                search_lower = ship_name.lower()
                matches = []
                
                for s in combined_data:
                    name = s.get("name", "").lower()
                    slug = s.get("slug", "").lower()
                    
                    if name == search_lower or slug == search_lower:
                        matches = [s] # Exact match found, stop searching
                        break
                    
                    if search_lower in name or search_lower in slug:
                        matches.append(s)
                
                if not matches:
                    return await ctx.send(f"No ships found matching '{ship_name}'. Try a more specific name like 'Avenger Titan'.")
                
                # If multiple matches, force selection
                if len(matches) > 1:
                     matches.sort(key=lambda x: len(x.get("name", "")))
                     view = ShipSelectionView(matches[:25], ctx.author, self.bot, ctx)
                     await ctx.send(f"Found {len(matches)} ships matching '{ship_name}'. Please select one:", view=view)
                     return

                ship = matches[0]

                # Reuse the embed builder from the view appropriately or inline logical
                # Since we can't easily access the View method without instantiating, 
                # let's just use the View to build it for consistency, or duplicate the logic slightly cleaned up.
                
                # We can just instantiate the view temporarily to use its builder, or better yet, make the builder static/standalone.
                # But for now, let's keep the inline logic consistent with the View's logic.
                
                s_name = ship.get("name", "Unknown Ship")
                s_desc = ship.get("description", "No description available.")
                s_focus = ship.get("focus", "N/A")
                s_size = ship.get("sizeLabel", "N/A")
                
                min_crew = ship.get("minCrew", "?")
                max_crew = ship.get("maxCrew", "?")
                s_crew = f"{min_crew} - {max_crew}" if min_crew != max_crew else str(min_crew)

                s_price = ship.get("priceLabel", "Not available in-game")
                s_pledge = ship.get("pledgePriceLabel", "N/A")
                
                # Images - Safely drill down
                media = ship.get("media", {})
                store_img = media.get("storeImage")
                
                img_main = None
                if isinstance(store_img, dict):
                    img_main = store_img.get("large") or store_img.get("medium") or store_img.get("source")
                elif isinstance(store_img, str):
                    img_main = store_img

                embed = discord.Embed(title=s_name, description=s_desc[:2048], color=discord.Color.dark_red())
                if img_main:
                    embed.set_image(url=img_main)
                
                manufacturer = ship.get("manufacturer", {}).get("name", "Unknown")
                
                embed.add_field(name="Manufacturer", value=manufacturer, inline=True)
                embed.add_field(name="Focus", value=s_focus, inline=True)
                embed.add_field(name="Size", value=s_size, inline=True)
                embed.add_field(name="Crew", value=s_crew, inline=True)
                embed.add_field(name="Price (aUEC)", value=str(s_price), inline=True)
                embed.add_field(name="Pledge", value=str(s_pledge), inline=True)
                
                slug = ship.get("slug")
                if slug:
                    embed.add_field(name="FleetYards Link", value=f"https://fleetyards.net/ships/{slug}", inline=False)
                    
                await ctx.send(embed=embed)

            except Exception as e:
                await ctx.send(f"An error occurred while fetching ship data: {e}")

    @sc_base.command(name="status")
    async def sc_status(self, ctx):
        """Check the current status of the Persistent Universe."""
        # The JSON API (incidents.json) is blocked (403). Using the public RSS XML feed instead.
        url = "https://status.robertsspaceindustries.com/index.xml"
        
        async with ctx.typing():
            try:
                # User-Agent is required to bypass basic bot protection
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
                
                async with self.session.get(url, headers=headers) as RP:
                    if RP.status == 200:
                        content = await RP.text()
                        
                        try:
                            # Basic XML parsing
                            root = ET.fromstring(content)
                            channel = root.find("channel")
                            items = channel.findall("item") if channel is not None else []
                            
                            if not items:
                                await ctx.send("No incidents reported recently.")
                                return
                            
                            # Get the most recent incident only
                            item = items[0]
                            title = item.find("title").text
                            link = item.find("link").text
                            description = item.find("description").text or "No details."
                            
                            # Simple cleanup of HTML tags commonly found in RSS descriptions
                            clean_desc = description.replace("<p>", "").replace("</p>", "\n").replace("<strong>", "**").replace("</strong>", "**").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
                            # Remove comments
                            clean_desc = clean_desc.replace("<!-- raw HTML omitted -->", "")
                            

                            if len(clean_desc) > 2048:
                                clean_desc = clean_desc[:2045] + "..."
                            
                            embed = discord.Embed(
                                title=title,
                                url=link,
                                description=clean_desc,
                                color=discord.Color.orange(),
                                timestamp=ctx.message.created_at
                            )
                            embed.set_author(name="RSI Platform Status", url="https://status.robertsspaceindustries.com/")
                            
                            await ctx.send(embed=embed)
                            

                        except ET.ParseError:
                             await ctx.send("Failed to parse status feed.")
                    else:
                        await ctx.send(f"Could not retrieve status from RSI. HTTP {RP.status}")
            except Exception as e:
                await ctx.send(f"An error occurred while fetching status: {e}")

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
        """Set the channel for automated RSI updates (Comm-Links)."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).tracked_channel.set(channel.id)
        await ctx.send(f"RSI website tracking has been enabled. Comm-Link updates will be posted in {channel.mention}.\nUse `[p]sc trackopts` to enable Roadmap or Dev Tracker alerts.")

    @sc_base.group(name="trackopts")
    @commands.has_permissions(manage_channels=True)
    async def sc_track_options(self, ctx):
        """Configure which RSI feeds to track (Roadmap, Dev Tracker)."""
        if ctx.invoked_subcommand is None:
             settings = await self.config.guild(ctx.guild).all()
             msg = "Current Tracking Settings:\n"
             msg += f"**Roadmap:** {'Enabled' if settings['track_roadmap'] else 'Disabled'}\n"
             msg += f"**Dev Tracker:** {'Enabled' if settings['track_devtracker'] else 'Disabled'}"
             await ctx.send(msg)

    @sc_track_options.command(name="roadmap")
    async def track_roadmap_toggle(self, ctx, toggle: bool):
        """Toggle Roadmap update tracking (True/False)."""
        await self.config.guild(ctx.guild).track_roadmap.set(toggle)
        await ctx.send(f"Roadmap tracking set to: {toggle}")

    @sc_track_options.command(name="devtracker")
    async def track_dev_toggle(self, ctx, toggle: bool):
        """Toggle Dev Tracker update tracking (True/False)."""
        await self.config.guild(ctx.guild).track_devtracker.set(toggle)
        await ctx.send(f"Dev Tracker updates set to: {toggle}")

    @tasks.loop(minutes=10.0)
    async def rsi_scraper_loop(self):
        """Periodic background loop for RSI website telemetry extraction."""
        await self.bot.wait_until_ready()
        
        feeds = [
            ("comm-link", "https://leonick.se/feeds/rsi/atom", self.config.last_comm_link_id, None), # Always tracks if channel set
            ("roadmap", "https://leonick.se/feeds/roadmap/atom", self.config.last_roadmap_id, "track_roadmap"),
            ("devtracker", "https://leonick.se/feeds/devtracker/atom", self.config.last_dev_id, "track_devtracker")
        ]
        
        for feed_type, feed_url, config_last_id, guild_config_key in feeds:
            try:
                # Ensure we have a session to use
                if self.session.closed:
                    self.session = aiohttp.ClientSession()

                async with self.session.get(feed_url) as response:
                    if response.status != 200:
                        continue
                    
                    xml_data = await response.text()
                    root = ET.fromstring(xml_data)
                    ns = {'atom': 'http://www.w3.org/2005/Atom'}
                    
                    latest_entry = root.find('atom:entry', ns)
                    if latest_entry is None:
                        continue
                        
                    entry_id = latest_entry.find('atom:id', ns).text
                    title = latest_entry.find('atom:title', ns).text
                    link = latest_entry.find('atom:link', ns).attrib['href']
                    content_element = latest_entry.find('atom:content', ns)
                    summary = content_element.text[:300] + "..." if content_element and content_element.text else "Click link for details."
                    
                    last_known_id = await config_last_id()
                    
                    if entry_id != last_known_id:
                        await config_last_id.set(entry_id)
                        
                        feed_titles = {
                            "comm-link": "New RSI Comm-Link",
                            "roadmap": "RSI Roadmap Update",
                            "devtracker": "RSI Dev Tracker Post"
                        }
                        
                        embed = discord.Embed(
                            title=feed_titles.get(feed_type, "RSI Update"),
                            description=f"**[{title}]({link})**\n\n{summary}",
                            color=discord.Color.gold()
                        )
                        embed.set_footer(text=f"Source: {feed_type.title()}")
                        
                        all_guilds = await self.config.all_guilds()
                        for guild_id, data in all_guilds.items():
                            channel_id = data.get("tracked_channel")
                            if channel_id:
                                # Check if this specific feed type is enabled for this guild
                                # Comm-links are enabled by default if a channel is set (guild_config_key is None)
                                if (guild_config_key is None) or (data.get(guild_config_key, False)):
                                    guild = self.bot.get_guild(guild_id)
                                    if guild:
                                        channel = guild.get_channel(channel_id)
                                        # Only send if the channel exists and we have permissions
                                        if channel and channel.permissions_for(guild.me).send_messages:
                                            await channel.send(embed=embed)
            except Exception as e:
                self.bot.logger.error(f"RSI Scraper Loop Exception ({feed_type}): {e}")
