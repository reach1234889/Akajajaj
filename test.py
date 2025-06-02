import random
import logging
import subprocess
import sys
import os
import re
import time
import concurrent.futures
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
from discord.ui import Button, View, Select
import string
from datetime import datetime, timedelta
from typing import Optional, Literal

TOKEN = ''
RAM_LIMIT = '32g'
SERVER_LIMIT = 100
database_file = 'database.txt'
PUBLIC_IP = '138.68.79.95'

# Admin user IDs - add your admin user IDs here
ADMIN_IDS = [875671806609084428, 1333473142039121990, 951418376230670336, 1295737579840340032]  # Replace with actual admin IDs

intents = discord.Intents.default()
intents.messages = False
intents.message_content = False

bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

# Helper functions
def is_admin(user_id):
    return user_id in ADMIN_IDS

def generate_random_string(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def generate_random_port(): 
    return random.randint(1025, 65535)

def parse_time_to_seconds(time_str):
    """Convert time string like '1d', '2h', '30m', '45s', '1y', '3M' to seconds"""
    if not time_str:
        return None
    
    units = {
        's': 1,               # seconds
        'm': 60,              # minutes
        'h': 3600,            # hours
        'd': 86400,           # days
        'M': 2592000,         # months (30 days)
        'y': 31536000         # years (365 days)
    }
    
    unit = time_str[-1]
    if unit in units and time_str[:-1].isdigit():
        return int(time_str[:-1]) * units[unit]
    elif time_str.isdigit():
        return int(time_str) * 86400  # Default to days if no unit specified
    return None

def format_expiry_date(seconds_from_now):
    """Convert seconds from now to a formatted date string"""
    if not seconds_from_now:
        return None
    
    expiry_date = datetime.now() + timedelta(seconds=seconds_from_now)
    return expiry_date.strftime("%Y-%m-%d %H:%M:%S")

def add_to_database(user, container_name, ssh_command, ram_limit=None, cpu_limit=None, creator=None, expiry=None, os_type="Ubuntu 22.04"):
    with open(database_file, 'a') as f:
        f.write(f"{user}|{container_name}|{ssh_command}|{ram_limit or '2048'}|{cpu_limit or '1'}|{creator or user}|{os_type}|{expiry or 'None'}\n")

def remove_from_database(container_id):
    if not os.path.exists(database_file):
        return
    with open(database_file, 'r') as f:
        lines = f.readlines()
    with open(database_file, 'w') as f:
        for line in lines:
            if container_id not in line:
                f.write(line)

def get_all_containers():
    if not os.path.exists(database_file):
        return []
    with open(database_file, 'r') as f:
        return [line.strip() for line in f.readlines()]

def get_container_stats(container_id):
    try:
        # Get memory usage
        mem_stats = subprocess.check_output(["docker", "stats", container_id, "--no-stream", "--format", "{{.MemUsage}}"]).decode().strip()
        
        # Get CPU usage
        cpu_stats = subprocess.check_output(["docker", "stats", container_id, "--no-stream", "--format", "{{.CPUPerc}}"]).decode().strip()
        
        # Get container status
        status = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", container_id]).decode().strip()
        
        return {
            "memory": mem_stats,
            "cpu": cpu_stats,
            "status": "🟢 Running" if status == "running" else "🔴 Stopped"
        }
    except Exception:
        return {"memory": "N/A", "cpu": "N/A", "status": "🔴 Stopped"}

def get_system_stats():
    try:
        # Get total memory usage
        total_mem = subprocess.check_output(["free", "-m"]).decode().strip()
        mem_lines = total_mem.split('\n')
        if len(mem_lines) >= 2:
            mem_values = mem_lines[1].split()
            total_mem = mem_values[1]
            used_mem = mem_values[2]
            
        # Get disk usage
        disk_usage = subprocess.check_output(["df", "-h", "/"]).decode().strip()
        disk_lines = disk_usage.split('\n')
        if len(disk_lines) >= 2:
            disk_values = disk_lines[1].split()
            total_disk = disk_values[1]
            used_disk = disk_values[2]
            
        return {
            "total_memory": f"{total_mem}GB",
            "used_memory": f"{used_mem}GB",
            "total_disk": total_disk,
            "used_disk": used_disk
        }
    except Exception as e:
        return {
            "total_memory": "N/A",
            "used_memory": "N/A",
            "total_disk": "N/A",
            "used_disk": "N/A",
            "error": str(e)
        }

async def capture_ssh_session_line(process):
    while True:
        output = await process.stdout.readline()
        if not output:
            break
        output = output.decode('utf-8').strip()
        if "ssh session:" in output:
            return output.split("ssh session:")[1].strip()
    return None

def get_ssh_command_from_database(container_id):
    if not os.path.exists(database_file):
        return None
    with open(database_file, 'r') as f:
        for line in f:
            if container_id in line:
                parts = line.strip().split('|')
                if len(parts) >= 3:
                    return parts[2]
    return None

def get_user_servers(user):
    if not os.path.exists(database_file):
        return []
    servers = []
    with open(database_file, 'r') as f:
        for line in f:
            if line.startswith(user):
                servers.append(line.strip())
    return servers

def count_user_servers(user):
    return len(get_user_servers(user))

def get_container_id_from_database(user, container_name=None):
    servers = get_user_servers(user)
    if servers:
        if container_name:
            for server in servers:
                parts = server.split('|')
                if len(parts) >= 2 and container_name in parts[1]:
                    return parts[1]
            return None
        else:
            return servers[0].split('|')[1]
    return None

# OS Selection dropdown for deploy command
class OSSelectView(View):
    def __init__(self, callback):
        super().__init__(timeout=60)
        self.callback = callback
        
        # Create the OS selection dropdown
        select = Select(
            placeholder="Select an operating system",
            options=[
                discord.SelectOption(label="Ubuntu 22.04", description="Latest LTS Ubuntu release", emoji="🐧", value="ubuntu"),
                discord.SelectOption(label="Debian 12", description="Stable Debian release", emoji="🐧", value="debian")
            ]
        )
        
        select.callback = self.select_callback
        self.add_item(select)
        
    async def select_callback(self, interaction: discord.Interaction):
        selected_os = interaction.data["values"][0]
        await interaction.response.defer()
        await self.callback(interaction, selected_os)

# Confirmation dialog class for delete operations
class ConfirmView(View):
    def __init__(self, container_id, container_name, is_delete_all=False):
        super().__init__(timeout=60)
        self.container_id = container_id
        self.container_name = container_name
        self.is_delete_all = is_delete_all
        
    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # First, acknowledge the interaction to prevent timeout
        await interaction.response.defer(ephemeral=False)
        
        try:
            if self.is_delete_all:
                # Delete all VPS instances
                containers = get_all_containers()
                deleted_count = 0
                
                for container_info in containers:
                    parts = container_info.split('|')
                    if len(parts) >= 2:
                        container_id = parts[1]
                        try:
                            subprocess.run(["docker", "stop", container_id], check=True, stderr=subprocess.DEVNULL)
                            subprocess.run(["docker", "rm", container_id], check=True, stderr=subprocess.DEVNULL)
                            deleted_count += 1
                        except Exception:
                            pass
                
                # Clear the database file
                with open(database_file, 'w') as f:
                    f.write('')
                    
                embed = discord.Embed(
                    title=" All VPS Instances Deleted",
                    description=f"Successfully deleted {deleted_count} VPS instances.",
                    color=0x00ff00
                )
                # Use followup instead of edit_message
                await interaction.followup.send(embed=embed)
                
                # Disable all buttons
                for child in self.children:
                    child.disabled = True
                
            else:
                # Delete single VPS instance
                try:
                    subprocess.run(["docker", "stop", self.container_id], check=True, stderr=subprocess.DEVNULL)
                    subprocess.run(["docker", "rm", self.container_id], check=True, stderr=subprocess.DEVNULL)
                    remove_from_database(self.container_id)
                    
                    embed = discord.Embed(
                        title=" VPS Deleted",
                        description=f"Successfully deleted VPS instance `{self.container_name}`.",
                        color=0x00ff00
                    )
                    # Use followup instead of edit_message
                    await interaction.followup.send(embed=embed)
                    
                    # Disable all buttons
                    for child in self.children:
                        child.disabled = True
                    
                except Exception as e:
                    embed = discord.Embed(
                        title="❌ Error",
                        description=f"Failed to delete VPS instance: {str(e)}",
                        color=0xff0000
                    )
                    await interaction.followup.send(embed=embed)
        except Exception as e:
            # Handle any unexpected errors
            try:
                await interaction.followup.send(f"An error occurred: {str(e)}")
            except:
                pass
    
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # First, acknowledge the interaction to prevent timeout
        await interaction.response.defer(ephemeral=False)
        
        embed = discord.Embed(
            title="🚫 Operation Cancelled",
            description="The delete operation has been cancelled.",
            color=0xffaa00
        )
        # Use followup instead of edit_message
        await interaction.followup.send(embed=embed)
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True

@bot.event
async def on_ready():
    change_status.start()
    print(f'🚀 Bot is ready. Logged in as {bot.user}')
    await bot.tree.sync()

@tasks.loop(seconds=5)
async def change_status():
    try:
        if os.path.exists(database_file):
            with open(database_file, 'r') as f:
                lines = f.readlines()
                instance_count = len(lines)
        else:
            instance_count = 0

        status = f"with {instance_count} Cloud Instances 🌐"
        await bot.change_presence(activity=discord.Game(name=status))
    except Exception as e:
        print(f"Failed to update status: {e}")

@bot.tree.command(name="manage_vps", description="🛠️ Manage your VPS instance with an interactive interface")
@app_commands.describe(container_name="The name of your VPS container")
async def manage_vps(interaction: discord.Interaction, container_name: str):
    user = str(interaction.user)
    container_id = get_container_id_from_database(user, container_name)

    if not container_id:
        embed = discord.Embed(
            title="❌ Not Found",
            description="No instance found with that name for your user.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    # Get container details from database
    container_details = None
    with open(database_file, 'r') as f:
        for line in f:
            if container_id in line:
                parts = line.strip().split('|')
                if len(parts) >= 8:
                    container_details = {
                        "user": parts[0],
                        "container_name": parts[1],
                        "ssh_command": parts[2],
                        "ram": parts[3],
                        "cpu": parts[4],
                        "creator": parts[5],
                        "os": parts[6],
                        "expiry": parts[7]
                    }
                break

    if not container_details:
        embed = discord.Embed(
            title="❌ Error",
            description="Could not retrieve VPS details from database.",
            color=0xff0000
        )
        await interaction.response.send_message(embed=embed)
        return

    # Get current stats
    stats = get_container_stats(container_id)

    # Create management view
    class ManagementView(View):
        def __init__(self):
            super().__init__(timeout=180)  # 3 minute timeout

        @discord.ui.button(label="▶️ Start", style=discord.ButtonStyle.success)
        async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await start_server(interaction, container_name)
            
        @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
        async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await stop_server(interaction, container_name)
            
        @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.primary)
        async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await restart_server(interaction, container_name)
            
        @discord.ui.button(label="🔄 Regen SSH", style=discord.ButtonStyle.secondary)
        async def regen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            await regen_ssh_command(interaction, container_name)
            
        @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
        async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            # Create confirmation dialog
            confirm_embed = discord.Embed(
                title="⚠️ Confirm Deletion",
                description=f"Are you sure you want to delete VPS instance `{container_name}`? This action cannot be undone.",
                color=0xffaa00
            )
            view = ConfirmView(container_id, container_name)
            await interaction.response.send_message(embed=confirm_embed, view=view)
            
            # Disable all buttons in original message
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)

    # Create embed with VPS details
    embed = discord.Embed(
        title=f"🛠️ Managing VPS: {container_name}",
        description=f"Status: {stats['status']}\nMemory: {stats['memory']}\nCPU: {stats['cpu']}",
        color=0x00aaff
    )
    
    embed.add_field(name="💾 RAM Allocation", value=f"{container_details['ram']}GB", inline=True)
    embed.add_field(name="🔥 CPU Cores", value=f"{container_details['cpu']} cores", inline=True)
    embed.add_field(name="🧊 OS", value=container_details['os'], inline=True)
    embed.add_field(name="⏱️ Expiry", value=container_details['expiry'] if container_details['expiry'] != 'None' else "Never", inline=True)
    embed.add_field(name="🔑 SSH Command", value=f"```{container_details['ssh_command']}```", inline=False)
    
    view = ManagementView()
    await interaction.response.send_message(embed=embed, view=view)

# [Rest of your existing commands and functions remain unchanged...]

bot.run(TOKEN)
