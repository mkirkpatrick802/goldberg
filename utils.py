from nextcord.ext import commands
from nextcord import Interaction

def user_has_any_role(interaction, role_names: list[str]) -> bool:
    if not interaction.guild:
        return False
    
    user_roles = {role.name for role in interaction.user.roles}
    return bool(user_roles & set(role_names))