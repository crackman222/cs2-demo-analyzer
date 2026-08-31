import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

async def download_hltv_demo(match_url):
    async with Stealth().use_async(async_playwright()) as p:
        # Launch a headed browser to better mimic human behavior
        browser = await p.chromium.launch(headless=False)
        
        # Create a browser context with human-like viewport and user agent
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1280, "height": 720}
        )
        
        page = await context.new_page()
        
        # Navigate to the specific HLTV match page
        print(f"Navigating to: {match_url}")
        await page.goto(match_url)
        
        # Wait to simulate human reading and let Cloudflare pass
        await page.wait_for_timeout(5000)
        
        # Listen for the download event trigger
        async with page.expect_download() as download_info:
            # Locate the GOTV/Demo download button element
            # Note: Selector may change if HLTV updates their HTML layout
            await page.click("a.stream-box[href*='/download/']")
        
        download = await download_info.value
        
        # Save the downloaded archive to your local folder
        save_path = f"./demos/{download.suggested_filename}"
        await download.save_as(save_path)
        print(f"Success! Demo downloaded to: {save_path}")
        
        await browser.close()

# Run the automation script
asyncio.run(download_hltv_demo("https://www.hltv.org/matches/2396932/g2-vs-spirit-blast-open-porto-2026"))