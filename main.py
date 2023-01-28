import asyncio
import climage
from src.bot import Bot 

splash = climage.convert('skip.png', width=120, is_unicode=True)
print(splash)
# print("Turning on the Money printer...")
# print("||====================================================================||")
# print("||--$--------------------------------------------------------------$--||")
# print("||(100)==================|  SKIP MONEY PRINTER  |================(100)||")
# print("||>>|          ~         '------========--------'                  |<<||")
# print("||<<|         /$\              // ____ \\                          |>>||")
# print("||>>|  12    //L\\            // ///..) \\         L38036133B   12 |<<||")
# print("||<<|        \\ //           || <||  >|  ||                        |>>||")
# print("||>>|         \$/            ||  $$ --/  ||        One Hundred     |<<||")
# print("||<<|      L38036133B        *\\  |__/  //* series                 |>>||")
# print("||>>|  12                     *\\/___\_//*   1989                  |<<||")
# print("||<<|      Treasurer     ______/MEV BOTS\________  Secretary 12    |>>||")
# print("||>>|                 ~| TO SKIP OR NOT TO SKIP |~                 |<<||")
# print("||(100)===================  BRRRRRRRRRRRRRRRRRR =================(100)||")
# print("||--$--------------------------------------------------------------$--||")
# print("||====================================================================||")
"""#############################################"""
"""@USER TODO: CHOOSE ENVIRONMENT VARIABLES PATH"""
ENV_FILE_PATH = "envs/juno.env"
#ENV_FILE_PATH = "envs/terra.env"
"""#############################################"""


async def main():
    """ Main entry point to run bot."""
    bot: Bot = Bot(env_file_path=ENV_FILE_PATH)
    await bot.init()
    await bot.run() 


if __name__ == "__main__":
    asyncio.run(main())