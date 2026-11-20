"""Reginald's coloured ASCII logo for the top-level command overview."""

from rich.console import Console
from rich.theme import Theme

_COLOURS = {
    "cap": "#9CAACB",
    "face": "#32A9E4",
    "eyes": "#FFFFFF",
    "pupils": "#8D9ABD",
    "tassel": "#FF6B01",
    "grille": "#488CB3",
}

_LOGO = r"""[cap].-----------------------------------[tassel].[/]----.
|===================================[tassel]||[/]===|
'---------.-------------------.-----[tassel]||[/]---'
          |                   |     [tassel]||[/]
          '-------------------'     [tassel]||[/][/]
[face]      .----------------------------.[tassel](_)[/]
    .'                              '.
   /      [eyes].----.          .----.[/]      \
  |      [eyes]/ .--. \        / .--. \ [/]     |
  |     [eyes]| |[pupils]##[/]o| |        | |[pupils]##[/]o| |[/]     |
  |     [eyes]| |[pupils]###[/]| |        | |[pupils]###[/]| |[/]     |
  |      [eyes]\ '--' /        \ '--' /[/]      |
  |       [eyes]'----'          '----'[/]       |
  |                                    |
  |     [grille].-----+-----+-----+-----.[/]      |
  |     [grille](_____|_____|_____|_____)[/]      |
   \                                  /
    '--------------------------------'[/]"""


def print_logo() -> None:
    console = Console(theme=Theme(_COLOURS))
    console.print(_LOGO, highlight=False, no_wrap=True, overflow="crop")
