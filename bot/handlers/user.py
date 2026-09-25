import html
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from bot.services.user_service import UserService
from bot.services.search_service import SearchService
from bot.services.blacklist_service import BlacklistService
from bot.models.models import User, UserStatus, RechargeRequest
from bot.keyboards.inline import (
    get_approval_keyboard,
    get_recharge_request_keyboard,
    get_recharge_amounts_keyboard,
    get_search_type_keyboard,
    get_phone_search_mode_keyboard,
    get_deep_search_agreement_keyboard
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.keyboards.reply import get_main_keyboard
from bot.config import config
from bot.middleware.daily_bonus import DAILY_BONUS_BANNER
import asyncio
import logging

logger = logging.getLogger(__name__)

class SearchStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_aadhar = State()
    waiting_for_email = State()
    waiting_for_username = State()

search_queue_count = 0
search_lock = asyncio.Lock()

router = Router()

def build_welcome_text(user: User, is_admin: bool) -> str:
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    effective_credits = UserService.get_effective_credits(user)

    if is_admin:
        quota_display = "♾️ Unlimited 👑"
        tier_display = "👑 System Administrator"
    elif user.has_active_subscription:
        rem = user.subscription_remaining_time
        days, hours = rem if rem else (0, 0)
        quota_display = f"♾️ Unlimited ({days}d {hours}h left)"
        tier_display = "💎 VIP Unlimited Pass"
    elif effective_credits > 0:
        parts = []
        if user.bonus_credits > 0:
            parts.append(f"🎁 {user.bonus_credits} daily (expires 23:59 IST)")
        if user.credits > 0:
            parts.append(f"🪙 {user.credits} permanent")
        quota_display = " | ".join(parts) if parts else str(effective_credits)
        tier_display = "🪙 Standard Operator"
    else:
        quota_display = "⚠️ 0 credits (Exhausted)"
        tier_display = "⏳ Daily Bonus Expired"

    raw_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Operator"
    name = html.escape(raw_name)

    return (
        "⚡ <b>BLACKSEARCH OSINT INTELLIGENCE</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Welcome, <b>{name}</b>!\n"
        "Your covert identity scanner and intelligence terminal.\n\n"
        "👤 <b>Account Overview:</b>\n"
        f"├ 🆔 <b>ID:</b> <code>{user.telegram_user_id}</code>\n"
        f"├ 🛡️ <b>Status:</b> <code>{user.status.value.upper()}</code>\n"
        f"├ 💎 <b>Tier:</b> {tier_display}\n"
        f"└ 💰 <b>Search Quota:</b> <b>{quota_display}</b>\n\n"
        "🚀 <b>Core Reconnaissance Modules:</b>\n"
        "• 📱 <b>Number Info</b> — Telecom operator, owner identity & leaked records\n"
        "• 🪪 <b>Aadhar Info</b> — Citizen demographics & linked registries\n"
        "• 🔬 <b>Deep Num Search (Beta)</b> — Aadhaar SIM pivot, family & household tracing\n"
        "• 📧 <b>Email Info</b> — Breach intelligence & 120+ social account scan\n"
        "• 👤 <b>Username Info</b> — Global footprint scanner across 400+ platforms\n\n"
        "👇 <i>Select an option from the menu below to begin:</i>"
    )

@router.callback_query(F.data == "verify_sub")
async def cb_verify_sub(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    from bot.services.channel_service import ChannelService
    from bot.keyboards.inline import get_force_sub_keyboard
    cs = ChannelService(session)
    missing = await cs.get_missing_channels(bot, callback.from_user.id)
    if missing:
        await callback.answer("❌ You haven't joined all required channels yet! Please join and try again.", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=get_force_sub_keyboard(missing))
        except Exception:
            pass
        return

    # Success! User joined all channels.
    await callback.answer("🎉 Verification successful! Access granted.", show_alert=True)
    try:
        await callback.message.delete()
    except Exception:
        pass

    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    is_new = False
    if not user:
        user = await user_service.create_user(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name
        )
        is_new = True

    # Alert admin about new verified user
    await ChannelService.notify_admins_new_user_verified(bot, session, user)

    # Process referral reward for referrer (if friend joined & verified)
    await user_service.process_referral_reward(bot, user)

    if is_new and user.bonus_credits > 0:
        await callback.message.answer(
            DAILY_BONUS_BANNER.format(amt=user.bonus_credits),
            parse_mode="HTML"
        )
    else:
        granted, amt, _ = await user_service.check_and_apply_daily_bonus(user)
        if granted:
            await callback.message.answer(
                DAILY_BONUS_BANNER.format(amt=amt),
                parse_mode="HTML"
            )

    is_admin = callback.from_user.id in config.admin_ids
    text = build_welcome_text(user, is_admin)
    await callback.message.answer(text, reply_markup=get_main_keyboard(is_admin), parse_mode="HTML")

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, bot: Bot):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    is_new = False
    
    if not user:
        # Check if started via referral link: e.g. /start ref_123456 or /start 123456
        referrer_id = None
        parts = (message.text or "").split()
        if len(parts) > 1:
            raw_ref = parts[1].strip()
            if raw_ref.startswith("ref_"):
                raw_ref = raw_ref[4:]
            if raw_ref.isdigit():
                parsed_ref = int(raw_ref)
                if parsed_ref != message.from_user.id:
                    referrer_id = parsed_ref

        user = await user_service.create_user(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            referred_by=referrer_id
        )
        is_new = True

    if user.status != UserStatus.APPROVED:
        return await message.answer("🔒 Your account is pending authorization or has been suspended.")

    is_admin = message.from_user.id in config.admin_ids

    # Check force subscription for non-admins
    if not is_admin:
        from bot.services.channel_service import ChannelService
        from bot.keyboards.inline import get_force_sub_keyboard
        from bot.middleware.forcesub import RESTRICTED_TEXT
        cs = ChannelService(session)
        missing = await cs.get_missing_channels(bot, message.from_user.id)
        if missing:
            return await message.answer(RESTRICTED_TEXT, reply_markup=get_force_sub_keyboard(missing), parse_mode="HTML")

    # If user has joined all channels and not yet marked verified, alert admin
    if not user.is_channel_verified:
        from bot.services.channel_service import ChannelService
        await ChannelService.notify_admins_new_user_verified(bot, session, user)

    # Process referral reward if user was referred and is now verified
    await user_service.process_referral_reward(bot, user)

    if is_new and user.bonus_credits > 0:
        await message.answer(
            DAILY_BONUS_BANNER.format(amt=user.bonus_credits),
            parse_mode="HTML"
        )

    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    effective_credits = UserService.get_effective_credits(user)
    welcome_text = build_welcome_text(user, is_admin)

    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(is_admin),
        parse_mode="HTML"
    )
    
    if not is_admin and effective_credits == 0 and not user.has_active_subscription:
        await message.answer(
            "⚠️ <b>Notice:</b> You have <b>0 search credits</b> remaining.\n"
            "Click <b>💳 Request Recharge</b> below to purchase search packs or activate Unlimited VIP access!",
            parse_mode="HTML"
        )

@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>BLACKSEARCH FIELD MANUAL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>User Commands:</b>\n"
        "• /start — Launch dashboard & refresh profile\n"
        "• /status — View credits, active VIP pass & usage metrics\n"
        "• /refer — Refer & Earn dashboard & get invite link\n"
        "• /search — Open interactive search selector\n"
        "• /recharge — Browse subscription packages & request top-ups\n"
        "• /help — Show this manual\n"
        "• /cancel — Terminate any ongoing search prompt\n\n"
        "💡 <b>Pro-Tips:</b>\n"
        "• You get a daily bonus every day on your first message!\n"
        "• Share your referral link (/refer) to earn permanent search credits!\n"
        "• Phone searches query multiple carrier and demographic registers in parallel.\n"
        "• Email searches scan 120+ platforms to find linked social accounts."
    )
    if message.from_user.id in config.admin_ids:
        text += (
            "\n\n<b>Admin Commands:</b>\n"
            "• /admin — Administrator dashboard\n"
            "• /plans — Manage subscription & credit packages\n"
            "• /channels — Manage force-sub channels\n"
            "• /dailybonus — Change daily free bonus credits\n"
            "• /referralreward — Set referral reward credits\n"
            "• /deleteuser &lt;id&gt; — Wipe user from DB to test restart\n"
            "• /broadcast &lt;msg&gt; — Send message to all users"
        )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    
    if not user:
        return await message.answer("Please type /start first.")

    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)
    is_admin = message.from_user.id in config.admin_ids
    import datetime
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    
    if is_admin:
        credits_display = "♾️ Unlimited 👑"
        plan_badge = "👑 System Administrator"
    elif user.has_active_subscription:
        rem = user.subscription_remaining_time
        days, hours = rem if rem else (0, 0)
        credits_display = f"♾️ Unlimited ({days}d {hours}h left)"
        plan_badge = "👑 VIP Unlimited Pass"
    elif effective_credits > 0:
        parts = []
        if user.bonus_credits > 0:
            parts.append(f"🎁 {user.bonus_credits} daily (expires 23:59 IST)")
        if user.credits > 0:
            parts.append(f"🪙 {user.credits} permanent")
        credits_display = " | ".join(parts) if parts else str(effective_credits)
        plan_badge = "🪙 Standard Operator"
    else:
        credits_display = "⚠️ 0 credits (Exhausted)"
        plan_badge = "⏳ Daily Bonus Expired"

    raw_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Operator"
    name = html.escape(raw_name)
    username_part = f"(@{html.escape(user.username)})" if user.username else ""
    created_date = user.created_at.strftime('%d %b %Y, %H:%M UTC') if user.created_at else "Unknown"

    status_text = (
        "📊 <b>OPERATOR STATUS REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Operator:</b> {name} {username_part}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.telegram_user_id}</code>\n"
        f"🛡️ <b>Account Status:</b> <code>{user.status.value.upper()}</code>\n\n"
        "💳 <b>Subscription & Quota:</b>\n"
        f"├ 💎 <b>Tier:</b> {plan_badge}\n"
        f"└ 💰 <b>Search Balance:</b> <b>{credits_display}</b>\n\n"
        "📈 <b>Usage Metrics:</b>\n"
        f"├ 🔍 <b>Searches Executed:</b> <code>{user.total_searches}</code> queries\n"
        f"├ 👥 <b>Friends Referred:</b> <code>{user.referral_count or 0}</code> verified users\n"
        f"└ 📅 <b>Member Since:</b> <code>{created_date}</code>"
    )
    await message.answer(status_text, parse_mode="HTML")

def build_referral_text(user: User, bot_username: str, reward: int) -> str:
    ref_link = f"https://t.me/{bot_username}?start=ref_{user.telegram_user_id}"
    ref_count = user.referral_count or 0
    earned_credits = user.referral_credits_earned or 0

    return (
        "👥 <b>REFER & EARN FREE SEARCH CREDITS</b> 👥\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Invite your friends to <b>BlackSearch OSINT Terminal</b> and earn permanent search credits for every verified friend who joins!\n\n"
        "🔗 <b>Your Exclusive Referral Link:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        "📊 <b>Your Referral Performance:</b>\n"
        f"├ 👥 <b>Verified Friends:</b> <code>{ref_count}</code> users\n"
        f"├ 💰 <b>Total Earned:</b> <code>+{earned_credits}</code> permanent credits\n"
        f"└ 🎁 <b>Reward per Referral:</b> <b>+{reward} Credits</b>\n\n"
        "🛡️ <b>Anti-Abuse Verification Rules:</b>\n"
        "1. Your friend must join using your unique referral link.\n"
        "2. Your friend <b>must have a public Telegram username</b>.\n"
        "3. Your friend <b>must join our required channel(s) and tap verify</b>.\n\n"
        "<i>⚡ Credits are added automatically to your permanent balance the moment your friend verifies!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

@router.message(F.text == "👥 Refer & Earn")
@router.message(Command("refer"))
@router.message(Command("referral"))
async def cmd_referral(message: Message, session: AsyncSession, bot: Bot):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user:
        return await message.answer("Please type /start first.")

    from bot.services.plan_service import PlanService
    from bot.keyboards.inline import get_referral_keyboard
    ps = PlanService(session)
    reward = await ps.get_referral_reward_credits()

    bot_me = await bot.get_me()
    bot_username = bot_me.username or "BlackSearchBot"

    text = build_referral_text(user, bot_username, reward)
    await message.answer(text, reply_markup=get_referral_keyboard(bot_username, user.telegram_user_id), parse_mode="HTML")

@router.callback_query(F.data == "ref_refresh")
async def cb_referral_refresh(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user:
        return await callback.answer("User not found.")

    from bot.services.plan_service import PlanService
    from bot.keyboards.inline import get_referral_keyboard
    ps = PlanService(session)
    reward = await ps.get_referral_reward_credits()

    bot_me = await bot.get_me()
    bot_username = bot_me.username or "BlackSearchBot"

    text = build_referral_text(user, bot_username, reward)
    try:
        await callback.message.edit_text(text, reply_markup=get_referral_keyboard(bot_username, user.telegram_user_id), parse_mode="HTML")
    except Exception:
        pass
    await callback.answer("Referral stats refreshed!")

@router.message(Command("search"))
async def cmd_search(message: Message, session: AsyncSession):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)

    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("🔒 You are not authorized to perform searches.")

    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    has_sub = user.has_active_subscription

    if effective_credits < 1 and not has_sub and message.from_user.id not in config.admin_ids:
        return await message.answer(
            "⚠️ <b>Search Quota Exhausted!</b>\n\n"
            "Please click <b>💳 Request Recharge</b> below to top up credits or activate an Unlimited VIP pass.\n"
            "<i>(Or return tomorrow for your daily bonus credits!)</i>",
            parse_mode="HTML"
        )
        
    await message.answer(
        "🔍 <b>Select a reconnaissance module:</b>",
        reply_markup=get_search_type_keyboard(),
        parse_mode="HTML"
    )

def build_deep_agreement_text(user: User, is_admin: bool) -> str:
    if is_admin:
        credits_display = "♾️ Unlimited 👑"
    elif user.has_active_subscription:
        rem = user.subscription_remaining_time
        days, hours = rem if rem else (0, 0)
        credits_display = f"♾️ Unlimited ({days}d {hours}h left)"
    else:
        effective_credits = UserService.get_effective_credits(user)
        parts = []
        if user.bonus_credits > 0:
            parts.append(f"🎁 {user.bonus_credits} daily")
        if user.credits > 0:
            parts.append(f"🪙 {user.credits} permanent")
        credits_display = " | ".join(parts) if parts else str(effective_credits)

    return (
        "🔬 <b>DEEP INTELLIGENCE RECONNAISSANCE [BETA]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>Investigation Cost:</b> <b>3 Credits</b>\n"
        "⏱️ <b>Estimated Duration:</b> ~15–30 seconds\n"
        f"📊 <b>Your Balance:</b> <b>{credits_display}</b>\n\n"
        "🔍 <b>Reconnaissance Scope:</b>\n"
        "├ 🪪 <b>Aadhaar Multi-SIM Reverse Pivot:</b> Uncovers all SIMs registered under citizen's Aadhaar.\n"
        "├ 👨‍👩‍👧‍👦 <b>Family & Lineage Intelligence:</b> Traces parental lineage, siblings, and verified co-habitants.\n"
        "├ 📞 <b>KYC Nominee Cross-Reference:</b> Reverse-resolves alternate contacts on telecom applications.\n"
        "└ 🛡️ <b>Digital Footprint & Breaches:</b> Deep archive & online exposure scan.\n\n"
        "⚠️ <b>DISCLAIMER & TERMS OF USAGE (BETA):</b>\n"
        "• <i>This module is in active <b>BETA testing</b> and algorithmic correlation is continuously being tuned.</i>\n"
        "• <i>Data is retrieved and linked dynamically from available telecom and citizen registries.</i>\n"
        "• <i><b>By tapping Agree below, you acknowledge and accept that we are NOT liable for any mismatched associations, incorrect linkages, or registry errors.</b></i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👇 <i>Do you accept the terms and agree to deduct 3 credits upon search?</i>"
    )

@router.message(F.text == "📱 Number Info")
async def btn_search_phone(message: Message, session: AsyncSession, state: FSMContext):
    """Direct Normal Phone Search (1 Credit)."""
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("❌ <b>You are not authorized.</b>", parse_mode="HTML")
    
    is_admin = message.from_user.id in config.admin_ids
    has_sub = user.has_active_subscription
    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    if not is_admin and not has_sub and effective_credits < 1:
        return await message.answer(
            "⚠️ <b>Search Quota Exhausted!</b>\n\n"
            "Normal Search requires <b>1 credit</b>, but your balance is <b>0</b>.\n"
            "Please request a recharge or return tomorrow for daily bonus credits.",
            reply_markup=get_recharge_request_keyboard(),
            parse_mode="HTML"
        )
        
    await state.set_state(SearchStates.waiting_for_phone)
    await state.update_data(search_mode="normal")

    if is_admin:
        credits_display = "♾️ Unlimited 👑"
    elif user.has_active_subscription:
        rem = user.subscription_remaining_time
        days, hours = rem if rem else (0, 0)
        credits_display = f"♾️ Unlimited ({days}d {hours}h left)"
    else:
        credits_display = f"{effective_credits} credit(s)"

    prompt_text = (
        "📱 <b>Phone Number Reconnaissance (Normal Search)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Cost:</b> 1 Credit  |  <b>Balance:</b> <b>{credits_display}</b>\n\n"
        "Enter the <b>10-digit Phone Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>9876543210</code> (no +91, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>"
    )
    await message.answer(prompt_text, parse_mode="HTML")

@router.message(F.text.in_({"🔬 Deep Num Search (Beta)", "🔬 Deep Num Search (BETA)", "Deep Num Search (Beta)", "🔬 Deep Search (Beta)"}))
async def btn_search_phone_deep(message: Message, session: AsyncSession, state: FSMContext):
    """Deep Phone Search button clicked -> triggers Disclaimer Agreement Modal (3 Credits)."""
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("❌ <b>You are not authorized.</b>", parse_mode="HTML")
    
    is_admin = message.from_user.id in config.admin_ids
    has_sub = user.has_active_subscription
    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    if not is_admin and not has_sub and effective_credits < 3:
        if effective_credits >= 1:
            builder = InlineKeyboardBuilder()
            builder.button(text="⚡ Switch to Normal Search (1 Credit)", callback_data="phone_mode:normal")
            builder.button(text="💳 Request Recharge", callback_data="request_recharge")
            builder.adjust(1)
            return await message.answer(
                f"⚠️ <b>Insufficient Credits for Deep Search!</b>\n\n"
                f"Deep Search requires <b>3 credits</b>, but your available balance is <b>{effective_credits} credit(s)</b>.\n\n"
                f"You can run a <b>Normal Search (1 credit)</b> or recharge credits below:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
        else:
            return await message.answer(
                "⚠️ <b>Search Quota Exhausted!</b>\n\n"
                "Deep Search requires <b>3 credits</b>, but your balance is <b>0</b>.\n"
                "Please request a recharge or return tomorrow for daily bonus credits.",
                reply_markup=get_recharge_request_keyboard(),
                parse_mode="HTML"
            )

    text = build_deep_agreement_text(user, is_admin)
    await message.answer(text, reply_markup=get_deep_search_agreement_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "deep_agree")
async def cb_deep_agree(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """User agreed to Beta disclaimer and 3 credit charge -> move to number input."""
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("Unauthorized.", show_alert=True)
        
    is_admin = callback.from_user.id in config.admin_ids
    has_sub = user.has_active_subscription
    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    if not is_admin and not has_sub and effective_credits < 3:
        return await callback.answer("Insufficient credits (3 required).", show_alert=True)

    await state.set_state(SearchStates.waiting_for_phone)
    await state.update_data(search_mode="deep")

    prompt_text = (
        "🔬 <b>Deep Phone Reconnaissance [BETA]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>Cost:</b> 3 Credits  |  <b>Status:</b> Agreement Accepted ✅\n\n"
        "Enter the <b>10-digit Phone Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>9876543210</code> (no +91, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>"
    )
    try:
        await callback.message.edit_text(prompt_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(prompt_text, parse_mode="HTML")
    await callback.answer("Agreement accepted! Enter phone number.")

@router.callback_query(F.data == "deep_cancel")
async def cb_deep_cancel(callback: CallbackQuery, state: FSMContext):
    """User declined disclaimer -> cancel gracefully."""
    await state.clear()
    cancel_text = (
        "❌ <b>Deep Search Cancelled</b>\n\n"
        "You declined the Beta terms and disclaimer. No credits were deducted.\n"
        "<i>You can use normal 📱 Number Info or request Deep Search again anytime.</i>"
    )
    try:
        await callback.message.edit_text(cancel_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(cancel_text, parse_mode="HTML")
    await callback.answer("Cancelled.")

@router.callback_query(F.data == "search_type_phone_deep")
async def cb_search_type_phone_deep(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Deep phone search triggered via /search inline menu."""
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("Unauthorized.", show_alert=True)
    is_admin = callback.from_user.id in config.admin_ids
    text = build_deep_agreement_text(user, is_admin)
    try:
        await callback.message.edit_text(text, reply_markup=get_deep_search_agreement_keyboard(), parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=get_deep_search_agreement_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "search_type_phone")
async def cb_search_type_phone(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Normal phone search triggered via /search inline menu."""
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("Unauthorized.", show_alert=True)
        
    is_admin = callback.from_user.id in config.admin_ids
    has_sub = user.has_active_subscription
    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    if not is_admin and not has_sub and effective_credits < 1:
        return await callback.answer("Insufficient credits (1 required).", show_alert=True)

    await state.set_state(SearchStates.waiting_for_phone)
    await state.update_data(search_mode="normal")

    if is_admin:
        credits_display = "♾️ Unlimited 👑"
    elif user.has_active_subscription:
        rem = user.subscription_remaining_time
        days, hours = rem if rem else (0, 0)
        credits_display = f"♾️ Unlimited ({days}d {hours}h left)"
    else:
        credits_display = f"{effective_credits} credit(s)"

    prompt_text = (
        "📱 <b>Phone Number Reconnaissance (Normal Search)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Cost:</b> 1 Credit  |  <b>Balance:</b> <b>{credits_display}</b>\n\n"
        "Enter the <b>10-digit Phone Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>9876543210</code> (no +91, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>"
    )
    try:
        await callback.message.edit_text(prompt_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(prompt_text, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("phone_mode:"))
async def cb_select_phone_mode(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    """Fallback handler for any legacy phone mode callbacks."""
    mode = callback.data.split(":")[1]

    if mode == "cancel":
        await state.clear()
        try:
            await callback.message.delete()
        except Exception:
            await callback.message.edit_text("❌ <b>Search cancelled.</b>", parse_mode="HTML")
        return await callback.answer("Cancelled.")

    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("You are not authorized.", show_alert=True)

    is_admin = callback.from_user.id in config.admin_ids
    has_sub = user.has_active_subscription
    await user_service.check_and_apply_daily_bonus(user)
    effective_credits = UserService.get_effective_credits(user)

    required_credits = 3 if mode == "deep" else 1

    if not is_admin and not has_sub and effective_credits < required_credits:
        if mode == "deep" and effective_credits >= 1:
            builder = InlineKeyboardBuilder()
            builder.button(text="⚡ Switch to Normal Search (1 Credit)", callback_data="phone_mode:normal")
            builder.button(text="💳 Request Recharge", callback_data="request_recharge")
            builder.adjust(1)
            await callback.message.answer(
                f"⚠️ <b>Insufficient Credits for Deep Search!</b>\n\n"
                f"Deep Search requires <b>3 credits</b>, but your available balance is <b>{effective_credits} credit(s)</b>.\n\n"
                f"You have enough balance to run a <b>Normal Search (1 credit)</b>, or recharge credits below:",
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            return await callback.answer()
        else:
            await callback.message.answer(
                f"⚠️ <b>Search Quota Exhausted!</b>\n\n"
                f"This search requires <b>{required_credits} credit(s)</b>, but your balance is <b>{effective_credits}</b>.\n"
                f"Please request a recharge or return tomorrow for your daily bonus credits.",
                reply_markup=get_recharge_request_keyboard(),
                parse_mode="HTML"
            )
            return await callback.answer()

    if mode == "deep":
        text = build_deep_agreement_text(user, is_admin)
        try:
            await callback.message.edit_text(text, reply_markup=get_deep_search_agreement_keyboard(), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=get_deep_search_agreement_keyboard(), parse_mode="HTML")
        return await callback.answer()

    await state.set_state(SearchStates.waiting_for_phone)
    await state.update_data(search_mode="normal")

    prompt_text = (
        "📱 <b>Normal Search Active</b> (Cost: <b>1 Credit</b>)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the <b>10-digit Phone Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>9876543210</code> (no +91, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>"
    )
    try:
        await callback.message.edit_text(prompt_text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(prompt_text, parse_mode="HTML")

    await callback.answer()

@router.callback_query(F.data == "search_type_aadhar")
async def cb_search_type_aadhar(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("Unauthorized.", show_alert=True)
    await state.set_state(SearchStates.waiting_for_aadhar)
    text = (
        "🪪 <b>Aadhaar Identity Lookup Module</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the <b>12-digit Aadhaar Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>123456789012</code> (numbers only, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@router.message(F.text == "🪪 Aadhar Info")
async def btn_aadhar_search(message: Message, session: AsyncSession, state: FSMContext):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("❌ <b>You are not authorized.</b>", parse_mode="HTML")
        
    await message.answer(
        "🪪 <b>Aadhaar Identity Lookup Module</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the <b>12-digit Aadhaar Number</b> to investigate:\n\n"
        "👉 <i>Format: <code>123456789012</code> (numbers only, no spaces)</i>\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML"
    )
    await state.set_state(SearchStates.waiting_for_aadhar)

@router.message(F.text == "📧 Email Info")
async def btn_email_search(message: Message, session: AsyncSession, state: FSMContext):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("❌ <b>You are not authorized.</b>", parse_mode="HTML")
        
    await message.answer(
        "📧 <b>Email OSINT & Account Scanner</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the <b>Target Email Address</b> to scan:\n\n"
        "👉 <i>Format: <code>target@gmail.com</code></i>\n"
        "<i>The engine will search database archives and check 120+ platforms.</i>\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML"
    )
    await state.set_state(SearchStates.waiting_for_email)

@router.message(F.text == "👤 Username Info")
async def btn_username_search(message: Message, session: AsyncSession, state: FSMContext):
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("❌ <b>You are not authorized.</b>", parse_mode="HTML")
        
    await message.answer(
        "👤 <b>Username Global Footprint Scanner</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Enter the <b>Username / Handle</b> to track:\n\n"
        "👉 <i>Format: <code>cyberrecon</code> (no @, no spaces)</i>\n"
        "<i>Deploys global footprint recon across 400+ online platforms worldwide.</i>\n"
        "<i>Send /cancel to abort.</i>",
        parse_mode="HTML"
    )
    await state.set_state(SearchStates.waiting_for_username)

@router.message(F.text == "📖 How to use")
async def btn_how_to_use(message: Message):
    text = (
        "🕵️‍♂️ <b>BLACKSEARCH OSINT FIELD GUIDE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Master the 5 powerful search modules at your disposal:\n\n"
        "📱 <b>1. Number Lookup (1 Credit)</b>\n"
        "• <b>What it does:</b> Cross-references telecom records, carrier registries, and breach archives.\n"
        "• <b>Format:</b> 10-digit number without country code or spaces.\n"
        "• <b>Example:</b> <code>9876543210</code>\n\n"
        "🔬 <b>2. Deep Num Search (Beta) (3 Credits)</b>\n"
        "• <b>What it does:</b> Deep algorithmic correlation tracing linked SIMs via Aadhaar pivot, household co-habitants, and verified family lineages.\n"
        "• <b>Format:</b> 10-digit number with disclaimer agreement confirmation.\n"
        "• <b>Note:</b> In active beta phase — algorithmic correlation is continually being tuned.\n\n"
        "🪪 <b>3. Aadhaar Lookup (1 Credit)</b>\n"
        "• <b>What it does:</b> Pulls deeply linked citizen identity and governmental leak datasets.\n"
        "• <b>Format:</b> 12-digit Aadhaar number.\n"
        "• <b>Example:</b> <code>123456789012</code>\n\n"
        "📧 <b>4. Email OSINT & Social Footprint (1 Credit)</b>\n"
        "• <b>What it does:</b> Searches leaks and scans 120+ platforms (Discord, Spotify, GitHub, etc.) to locate active accounts.\n"
        "• <b>Format:</b> Complete email address.\n"
        "• <b>Example:</b> <code>target@gmail.com</code>\n\n"
        "👤 <b>5. Username Global Recon (1 Credit)</b>\n"
        "• <b>What it does:</b> Deploys our global reconnaissance scanner across 400+ social networks and platforms worldwide.\n"
        "• <b>Format:</b> Username handle without spaces.\n"
        "• <b>Example:</b> <code>cyberrecon</code>\n\n"
        "💰 <b>Need More Quota?</b>\n"
        "Click <b>💳 Request Recharge</b> below to activate instant credits or an Unlimited VIP Pass."
    )
    await message.answer(text, parse_mode="HTML")

@router.message(F.text == "📊 My Status")
async def btn_status(message: Message, session: AsyncSession):
    await cmd_status(message, session)

@router.message(F.text == "💳 Request Recharge")
async def btn_recharge(message: Message, session: AsyncSession, bot: Bot):
    await cmd_recharge(message, session, bot)

def _chunk_text(text: str, max_size: int = 3800) -> list[str]:
    """Splits text into chunks no larger than max_size, breaking at newlines if possible."""
    chunks = []
    while len(text) > max_size:
        split_at = text.rfind("\n", 0, max_size)
        if split_at == -1 or split_at < max_size // 3:
            split_at = max_size
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\r\n")
    if text:
        chunks.append(text)
    return chunks

async def send_long_search_result(
    message: Message,
    header: str,
    body: str,
    footer: str
):
    """Sends search results to the user, safely splitting across multiple messages if exceeding Telegram 4096-char limit."""
    full_text = f"{header}{body}{footer}"
    if len(full_text) <= 4000:
        try:
            return await message.answer(full_text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            return await message.answer(full_text, disable_web_page_preview=True)

    # Split by record blocks if present
    if "<b>--- Record " in body:
        parts = body.split("<b>--- Record ")
        chunks = []
        cur = header + parts[0]
        for p in parts[1:]:
            rec = "<b>--- Record " + p
            if len(cur) + len(rec) < 3700:
                cur += rec
            else:
                chunks.append(cur)
                cur = rec
        chunks.append(cur + footer)
    else:
        # Split by lines
        lines = body.splitlines(keepends=True)
        chunks = []
        cur = header
        for line in lines:
            if len(cur) + len(line) < 3700:
                cur += line
            else:
                chunks.append(cur)
                cur = line
        chunks.append(cur + footer)

    for idx, c in enumerate(chunks):
        if not c.strip():
            continue
        sub_chunks = _chunk_text(c, 3800)
        for sc in sub_chunks:
            try:
                await message.answer(sc, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await message.answer(sc, disable_web_page_preview=True)
            await asyncio.sleep(0.08)

@router.message(SearchStates.waiting_for_phone)
@router.message(SearchStates.waiting_for_aadhar)
@router.message(SearchStates.waiting_for_email)
@router.message(SearchStates.waiting_for_username)
async def process_search_input(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    if not message.text:
        return await message.answer("⚠️ Please send text input.")

    query = message.text.strip()
    is_admin = message.from_user.id in config.admin_ids

    # Navigation buttons: immediately execute the clicked action instead of requiring double-tap
    NAV_ACTIONS = {
        "📱 Number Info": lambda: btn_search_phone(message, session, state),
        "🪪 Aadhar Info": lambda: btn_aadhar_search(message, session, state),
        "🔬 Deep Num Search (Beta)": lambda: btn_search_phone_deep(message, session, state),
        "🔬 Deep Num Search (BETA)": lambda: btn_search_phone_deep(message, session, state),
        "Deep Num Search (Beta)": lambda: btn_search_phone_deep(message, session, state),
        "🔬 Deep Search (Beta)": lambda: btn_search_phone_deep(message, session, state),
        "📧 Email Info": lambda: btn_email_search(message, session, state),
        "👤 Username Info": lambda: btn_username_search(message, session, state),
        "📊 My Status": lambda: cmd_status(message, session),
        "👥 Refer & Earn": lambda: cmd_referral(message, session, bot),
        "📖 How to use": lambda: btn_how_to_use(message),
        "💳 Request Recharge": lambda: cmd_recharge(message, session, bot),
    }

    ADMIN_NAV_BUTTONS = [
        "⚙️ Manage Users", "⚙ Manage Users", "Manage Users",
        "💰 Manage Points", "📦 Manage Plans", "📢 Channels", "🚫 Blocklist"
    ]

    if query in NAV_ACTIONS:
        await state.clear()
        return await NAV_ACTIONS[query]()
    elif is_admin and query in ADMIN_NAV_BUTTONS:
        await state.clear()
        from bot.handlers.admin import (
            btn_manage_users,
            btn_manage_points,
            cmd_manage_plans,
            cmd_manage_channels,
            cmd_blocklist
        )
        admin_actions = {
            "⚙️ Manage Users": lambda: btn_manage_users(message, session),
            "⚙ Manage Users": lambda: btn_manage_users(message, session),
            "Manage Users": lambda: btn_manage_users(message, session),
            "💰 Manage Points": lambda: btn_manage_points(message, session),
            "📦 Manage Plans": lambda: cmd_manage_plans(message, session, state),
            "📢 Channels": lambda: cmd_manage_channels(message, session, state),
            "🚫 Blocklist": lambda: cmd_blocklist(message, session, state),
        }
        return await admin_actions[query]()

    current_state = await state.get_state()

    if current_state == SearchStates.waiting_for_phone.state:
        search_type = "phone"
        # Auto-normalize phone numbers: strip +91, country code 91 if 12 digits, spaces, hyphens
        raw_digits = "".join(ch for ch in query if ch.isdigit())
        if raw_digits.startswith("91") and len(raw_digits) == 12:
            raw_digits = raw_digits[2:]
        if len(raw_digits) == 10:
            query = raw_digits
        else:
            return await message.answer(
                "⚠️ <b>Invalid Phone Number</b>\n\n"
                "Please enter a valid 10-digit Indian mobile number.\n"
                "<i>Examples: <code>9876543210</code>, <code>+91 98765 43210</code></i>\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="HTML"
            )
    elif current_state == SearchStates.waiting_for_email.state:
        search_type = "email"
        clean_email = query.strip().lower()
        if " " in clean_email or "@" not in clean_email or "." not in clean_email.split("@")[-1]:
            return await message.answer(
                "⚠️ <b>Invalid Email Address</b>\n\n"
                "Please enter a valid email address.\n"
                "<i>Example: <code>target@gmail.com</code></i>\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="HTML"
            )
        query = clean_email
    elif current_state == SearchStates.waiting_for_username.state:
        search_type = "username"
        clean_uname = query.lstrip("@").strip()
        if " " in clean_uname or not clean_uname:
            return await message.answer(
                "⚠️ <b>Invalid Username</b>\n\n"
                "Please enter a valid username handle without spaces.\n"
                "<i>Example: <code>cyberrecon</code> (without @)</i>\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="HTML"
            )
        query = clean_uname
    else:
        search_type = "aadhar"
        raw_digits = "".join(ch for ch in query if ch.isdigit())
        if len(raw_digits) == 12:
            query = raw_digits
        else:
            return await message.answer(
                "⚠️ <b>Invalid Aadhaar Number</b>\n\n"
                "Please enter a valid 12-digit Aadhaar number.\n"
                "<i>Example: <code>123456789012</code></i>\n"
                "<i>Send /cancel to abort.</i>",
                parse_mode="HTML"
            )

    state_data = await state.get_data()
    search_mode = state_data.get("search_mode", "normal")
    await state.clear()

    # Determine credit cost based on mode: Deep Search = 3 credits, all others = 1 credit
    credit_cost = 3 if (search_type == "phone" and search_mode == "deep") else 1

    # ── Blocklist Search Interception ──
    bl_service = BlacklistService(session)
    if await bl_service.is_blacklisted(search_type, query):
        type_labels = {
            "phone": "Phone number",
            "email": "Email address",
            "username": "Username",
            "aadhar": "Aadhaar number",
        }
        label = type_labels.get(search_type, "Entity")
        safe_query = html.escape(query)
        return await message.answer(
            f"⛔ <b>SEARCH RESTRICTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Restricted Target:</b>\n"
            f"This {label.lower()} (<code>{safe_query}</code>) is <b>blacklisted</b> from searches by system administration.\n\n"
            f"🔒 <i>Due to administrative privacy policy, intelligence records for this entity are permanently restricted.</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i><b>Notice:</b> No search credits have been deducted from your account.</i>",
            parse_mode="HTML"
        )

    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(message.from_user.id)

    if not user or user.status != UserStatus.APPROVED:
        return await message.answer("You are not authorized to perform searches.")

    deduction_info = None
    has_sub = False
    if not is_admin:
        has_sub = user.has_active_subscription
        await user_service.check_and_apply_daily_bonus(user)
        effective_credits = UserService.get_effective_credits(user)

        if effective_credits < credit_cost and not has_sub:
            if credit_cost == 3 and effective_credits >= 1:
                builder = InlineKeyboardBuilder()
                builder.button(text="⚡ Switch to Normal Search (1 Credit)", callback_data="phone_mode:normal")
                builder.button(text="💳 Request Recharge", callback_data="request_recharge")
                builder.adjust(1)
                return await message.answer(
                    f"⚠️ <b>Insufficient Credits for Deep Search!</b>\n\n"
                    f"Deep Search requires <b>3 credits</b>, but your current balance is <b>{effective_credits} credit(s)</b>.\n\n"
                    f"You have enough balance to run a <b>Normal Search (1 credit)</b>, or recharge credits below:",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML"
                )
            else:
                return await message.answer(
                    f"⚠️ <b>Search Quota Exhausted!</b>\n\n"
                    f"This search requires <b>{credit_cost} credit(s)</b>, but your available balance is <b>{effective_credits}</b>.\n"
                    f"Please request a recharge or return tomorrow for your daily bonus credits.",
                    reply_markup=get_recharge_request_keyboard(),
                    parse_mode="HTML"
                )

        if not has_sub:
            # Deduct credit safely, tracking exact bonus vs permanent credits used
            success, deduction_info = await user_service.deduct_credit(user.telegram_user_id, amount=credit_cost)
            if not success:
                return await message.answer("Failed to process credits. Please try again.")

    global search_queue_count
    search_queue_count += 1
    try:
        coffee_msg = None
        if search_type == "phone" and search_mode == "deep":
            coffee_msg = await message.answer(
                "☕ <b>Grab your coffee!</b> 🔬 [BETA]\n\n"
                "<i>Deep Search is launching heavy multi-hop reconnaissance across 97GB+ of intelligence databases, tracing linked SIMs, household registries, and digital archives...\n"
                "This may take a few moments!</i>\n\n"
                "⚠️ <i><b>Note:</b> Deep Search is in <b>BETA phase</b>. We are still actively optimizing algorithms and are not responsible for any inaccuracies or wrong results.</i>",
                parse_mode="HTML"
            )

        # Since MotherDuck can easily handle 15 active searches at once, the first 15 people are NOT in a queue!
        if search_queue_count > 15:
            wait_msg = await message.answer(f"⏳ <b>You are in a queue!</b>\nPeople ahead of you: {search_queue_count - 15}\n<i>I will notify you when your search is over.</i>", parse_mode="HTML")
        else:
            if search_type == "phone" and search_mode == "deep":
                wait_msg = await message.answer("🔬 <b>Initializing Deep Intelligence Scan [BETA]...</b>\n<code>[          ]</code>", parse_mode="HTML")
            else:
                wait_msg = await message.answer("⏳ <b>Querying Global Database, please wait...</b>\n<code>[          ]</code>", parse_mode="HTML")

        search_service = SearchService(session)
        if search_type == "phone" and search_mode == "deep":
            search_task = asyncio.create_task(search_service.search_deep_phone(user, phone=query))
        else:
            search_task = asyncio.create_task(search_service.search(user, query=query, search_type=search_type))

        frames = [
            "[=         ]",
            "[==        ]",
            "[===       ]",
            "[====      ]",
            "[=====     ]",
            "[======    ]",
            "[=======   ]",
            "[========  ]",
            "[========= ]",
            "[==========]"
        ]

        if search_queue_count > 15:
            animation_texts = [
                f"⏳ <b>You are in a queue!</b> ({search_queue_count - 15} ahead of you)\n<i>I will notify you when your search is over.</i>"
            ]
        elif search_type == "phone" and search_mode == "deep":
            animation_texts = [
                "🔬 <b>Initializing Deep Intelligence Scan [BETA]...</b>",
                "🔄 <b>Executing Reverse Aadhaar SIM Pivot...</b>",
                "👨‍👩‍👧‍👦 <b>Correlating Household & Family Linkages...</b>",
                "🛡️ <b>Scanning Digital Archives & Online Footprint...</b>"
            ]
        elif search_type == "email":
            animation_texts = [
                "⏳ <b>Searching Personal Database...</b>",
                "⏳ <b>Scanning 120+ Social Media Sites...</b>",
                "⏳ <b>Checking Linked Accounts...</b>",
                "⏳ <b>Cross-referencing Open Source data...</b>"
            ]
        elif search_type == "username":
            animation_texts = [
                "⏳ <b>Initializing OSINT Engine...</b>",
                "⏳ <b>Scanning 400+ Social Media Platforms...</b>",
                "⏳ <b>Extracting Active Profiles...</b>",
                "⏳ <b>Cross-referencing Open Source data...</b>"
            ]
        else:
            animation_texts = [
                "⏳ <b>Querying Global Database, please wait...</b>"
            ]

        frame_idx = 0

        # Animate loading bar while search is running
        while not search_task.done():
            for _ in range(25): # check every 0.1s for 2.5s total before updating frame
                if search_task.done():
                    break
                await asyncio.sleep(0.1)

            if not search_task.done():
                frame_idx = (frame_idx + 1) % len(frames)
                text_idx = frame_idx % len(animation_texts)
                msg_text = animation_texts[text_idx]

                try:
                    await wait_msg.edit_text(f"{msg_text}\n<code>{frames[frame_idx]}</code>", parse_mode="HTML")
                except Exception:
                    pass

        result = search_task.result()

        # Delete waiting message and coffee notice
        try:
            await wait_msg.delete()
        except Exception:
            pass
        if coffee_msg:
            try:
                await coffee_msg.delete()
            except Exception:
                pass
    finally:
        search_queue_count -= 1

    if result["success"]:
        import datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if is_admin:
            credits_display = "Unlimited 👑"
        elif user.has_active_subscription:
            credits_display = f"Unlimited ({user.subscription_end.strftime('%Y-%m-%d')})"
        else:
            effective_credits = UserService.get_effective_credits(user)
            parts = []
            if user.bonus_credits > 0:
                parts.append(f"🎁 {user.bonus_credits} daily")
            if user.credits > 0:
                parts.append(f"🪙 {user.credits} permanent")
            credits_display = " | ".join(parts) if parts else "0"

        if search_type == "phone" and search_mode == "deep":
            header = ""
            footer = f"\n💰 <b>Remaining Balance:</b> <code>{credits_display}</code>"
        else:
            header = "✅ <b>Search Successful!</b>\n\n"
            footer = f"\n\n💰 <b>Remaining credits:</b> <code>{credits_display}</code>"
        await send_long_search_result(message, header, result['data'], footer)
    else:
        # If search failed or yielded no results, auto-refund the deducted credits!
        if not is_admin and not has_sub and deduction_info:
            await user_service.refund_deduction(user.telegram_user_id, deduction_info)
            refund_note = f"\n\n💰 <i>Your {credit_cost} search credit(s) have been automatically refunded.</i>"
        else:
            refund_note = ""
        await message.answer(f"❌ {result.get('data', 'Database temporarily unavailable.')}{refund_note}", parse_mode="HTML")

from bot.keyboards.inline import get_payment_packages_keyboard
from bot.services.plan_service import PlanService
from bot.models.models import PlanType

@router.message(Command("recharge"))
async def cmd_recharge(message: Message, session: AsyncSession, bot: Bot):
    if message.from_user.id in config.admin_ids:
        return await message.answer("You are an Admin! You have unlimited credits and do not need to recharge. 👑")
    
    plan_service = PlanService(session)
    plans = await plan_service.get_all_plans(active_only=True)
    
    text = (
        "👑 <b>Buy Credits or Unlimited Plans</b>\n\n"
        "To purchase, please contact the owner @Heb47.\n\n"
        "Select the package you want to buy below to send a purchase request to the admin:"
    )
    await message.answer(text, reply_markup=get_payment_packages_keyboard(plans), parse_mode="HTML")

@router.callback_query(F.data == "request_recharge")
async def cb_request_recharge(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    """Handler for the 'Request Recharge' inline button shown when credits run out.
    Opens the package selection menu with active plans from database.
    """
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("You are not authorized.", show_alert=True)

    if callback.from_user.id in config.admin_ids:
        return await callback.answer("You are an Admin with unlimited credits! 👑", show_alert=True)

    plan_service = PlanService(session)
    plans = await plan_service.get_all_plans(active_only=True)

    text = (
        "👑 <b>Buy Credits or Unlimited Plans</b>\n\n"
        "To purchase, please contact the owner @Heb47.\n\n"
        "Select the package you want to buy below to send a purchase request to the admin:"
    )
    await callback.message.answer(text, reply_markup=get_payment_packages_keyboard(plans), parse_mode="HTML")
    await callback.answer()  # dismiss the button spinner


async def notify_admins_purchase_request(
    bot: Bot,
    req: RechargeRequest,
    user: User,
    pkg_name: str,
    is_updated: bool = False
) -> int:
    """Dispatches purchase request notification to all configured admins with robust fallback and logging."""
    from bot.keyboards.inline import get_recharge_approval_keyboard
    import html

    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "User"
    safe_name = html.escape(full_name)
    username_str = f"@{user.username}" if user.username else "None"
    safe_username = html.escape(username_str)
    safe_pkg = html.escape(pkg_name)

    title_prefix = "🔄 <b>Updated Purchase Request</b>" if is_updated else "💳 <b>New Purchase Request</b>"

    html_text = (
        f"{title_prefix} <b>#{req.id}</b>\n\n"
        f"👤 <b>Name:</b> <a href='tg://user?id={user.telegram_user_id}'>{safe_name}</a>\n"
        f"🔗 <b>Username:</b> {safe_username}\n"
        f"🆔 <b>User ID:</b> <code>{user.telegram_user_id}</code>\n\n"
        f"📦 <b>Package Selected:</b> <b>{safe_pkg}</b>"
    )

    plain_title = "🔄 Updated Purchase Request" if is_updated else "💳 New Purchase Request"
    plain_text = (
        f"{plain_title} #{req.id}\n\n"
        f"👤 Name: {full_name}\n"
        f"🔗 Username: {username_str}\n"
        f"🆔 User ID: {user.telegram_user_id}\n\n"
        f"📦 Package Selected: {pkg_name}"
    )

    notified_count = 0
    reply_markup = get_recharge_approval_keyboard(req.id)

    for admin_id in config.admin_ids:
        # First attempt: HTML formatted message
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=html_text,
                reply_markup=reply_markup,
                parse_mode="HTML"
            )
            logger.info(f"Dispatched purchase request #{req.id} to admin {admin_id} (HTML mode)")
            notified_count += 1
            continue
        except Exception as e_html:
            logger.warning(f"Failed to send purchase request #{req.id} in HTML mode to admin {admin_id}: {e_html}. Retrying in plain text...")

        # Fallback attempt: Plain text
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=plain_text,
                reply_markup=reply_markup
            )
            logger.info(f"Dispatched purchase request #{req.id} to admin {admin_id} (Plain text mode)")
            notified_count += 1
        except Exception as e_plain:
            logger.error(f"Failed to send purchase request #{req.id} to admin {admin_id} in plain text mode: {e_plain}")

    return notified_count


@router.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    plan_id = int(callback.data.split("_")[2])
    
    user_service = UserService(session)
    plan_service = PlanService(session)
    
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("You are not authorized.", show_alert=True)
        
    plan = await plan_service.get_plan_by_id(plan_id)
    if not plan or not plan.is_active:
        return await callback.answer("This plan is no longer available. Please select another.", show_alert=True)
        
    amount_val = plan.credits if plan.plan_type == PlanType.CREDITS else -plan.days
    req = await user_service.request_recharge(callback.from_user.id, amount_val, plan_id=plan.id)
    if not req:
        return await callback.answer("Failed to create purchase request. Please try again.", show_alert=True)

    is_updated = getattr(req, "is_updated", False)
    pkg_name = f"{plan.name} (₹{plan.price})"

    if is_updated:
        await callback.answer("🔄 Request updated! Please contact @Heb47 to pay.")
        await callback.message.edit_text(
            f"🔄 <b>Purchase Request Updated!</b>\n\nYou updated your selection to: <b>{pkg_name}</b>\n\n"
            "👉 <b>Please message @Heb47 to complete your payment.</b>\n"
            "Once payment is confirmed, your account will be upgraded instantly!",
            parse_mode="HTML"
        )
    else:
        await callback.answer("✅ Request sent! Please contact @Heb47 to pay.")
        await callback.message.edit_text(
            f"✅ <b>Purchase Request Sent!</b>\n\nYou selected: <b>{pkg_name}</b>\n\n"
            "👉 <b>Please message @Heb47 to complete your payment.</b>\n"
            "Once payment is confirmed, your account will be upgraded instantly!",
            parse_mode="HTML"
        )

    await notify_admins_purchase_request(bot, req, user, pkg_name, is_updated=is_updated)


@router.callback_query(F.data.startswith("buy_package_"))
async def cb_buy_package(callback: CallbackQuery, session: AsyncSession, bot: Bot):
    package_val = int(callback.data.split("_")[2])
    
    user_service = UserService(session)
    user = await user_service.get_user_by_telegram_id(callback.from_user.id)
    if not user or user.status != UserStatus.APPROVED:
        return await callback.answer("You are not authorized.", show_alert=True)
        
    req = await user_service.request_recharge(callback.from_user.id, package_val)
    if not req:
        return await callback.answer("Failed to create purchase request. Please try again.", show_alert=True)
        
    if package_val == 15: pkg_name = "₹50 for 15 searches"
    elif package_val == 40: pkg_name = "₹100 for 40 searches"
    elif package_val == -1: pkg_name = "₹200 for 1 day unlimited"
    elif package_val == -7: pkg_name = "₹700 for 7 days unlimited"
    else: pkg_name = f"{package_val} credits"

    is_updated = getattr(req, "is_updated", False)

    if is_updated:
        await callback.answer("🔄 Request updated! Please contact @Heb47 to pay.")
        await callback.message.edit_text(
            f"🔄 <b>Purchase Request Updated!</b>\n\nYou updated your selection to: <b>{pkg_name}</b>\n\n"
            "👉 <b>Please message @Heb47 to complete your payment.</b>\n"
            "Once payment is confirmed, your account will be upgraded instantly!",
            parse_mode="HTML"
        )
    else:
        await callback.answer("✅ Request sent! Please contact @Heb47 to pay.")
        await callback.message.edit_text(
            f"✅ <b>Purchase Request Sent!</b>\n\nYou selected: <b>{pkg_name}</b>\n\n"
            "👉 <b>Please message @Heb47 to complete your payment.</b>\n"
            "Once payment is confirmed, your account will be upgraded instantly!",
            parse_mode="HTML"
        )

    await notify_admins_purchase_request(bot, req, user, pkg_name, is_updated=is_updated)

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id in config.admin_ids
    await message.answer("❌ Current operation cancelled.", reply_markup=get_main_keyboard(is_admin))
