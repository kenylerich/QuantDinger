"""
Market API routes (local-only).
Provides watchlist, market metadata, symbol search, and pricing helpers for the frontend.
"""
from flask import g, jsonify, request
from app.openapi.blueprint import HumanBlueprint as Blueprint
import traceback
import json

from app.utils.logger import get_logger
from app.utils.config_loader import load_addon_config
from app.utils.auth import login_required
from app.services.market.quotes import get_price_map, get_single_price
from app.services.market.symbol_search import (
    get_hot_symbols as search_hot_symbols,
    search_market_symbols,
)
from app.services.market.watchlist import (
    add_watchlist_item,
    add_watchlist_items,
    create_watchlist_group,
    delete_watchlist_group,
    get_user_watchlist_pairs,
    list_watchlist,
    list_watchlist_groups,
    move_watchlist_items,
    normalize_symbol,
    remove_watchlist_item,
    set_watchlist_order,
    update_watchlist_group,
    validate_watchlist_pair,
)
from app.services.market_context import default_crypto_exchange_id
from app.utils.request_guard import RequestGuardError, cache_key, guarded_cached
from app.utils.market_visibility import filter_market_items

logger = get_logger(__name__)

market_blp = Blueprint('market', __name__)

def _ensure_watchlist_table():
    # Table is created by db schema init; this is only a sanity hook.
    return True

@market_blp.route('/config', methods=['GET'])
def get_public_config():
    """
    Public config for frontend (local mode).
    Mirrors the old PHP `/addons/quantdinger/index/getConfig` shape.
    """
    try:
        cfg = load_addon_config()
        models = (cfg.get('ai', {}) or {}).get('models')
        if not isinstance(models, dict) or not models:
            # Fallback defaults (offline friendly)
            models = {
                # Unified frontend model list (OpenRouter-style ids)
                'openai/gpt-5.4': 'OpenAI: GPT-5.4',
                'x-ai/grok-code-fast-1': 'xAI: Grok Code Fast 1',
                'x-ai/grok-4-fast': 'xAI: Grok 4 Fast',
                'x-ai/grok-4.1-fast': 'xAI: Grok 4.1 Fast',
                'google/gemini-2.5-flash': 'Google: Gemini 2.5 Flash',
                'google/gemini-2.0-flash-001': 'Google: Gemini 2.0 Flash',
                'google/gemini-3-pro-preview': 'Google: Gemini 3 Pro Preview',
                'google/gemini-2.5-flash-lite': 'Google: Gemini 2.5 Flash Lite',
                'google/gemini-2.5-pro': 'Google: Gemini 2.5 Pro',
                'openai/gpt-4o': 'OpenAI: GPT-4o',
                'openai/gpt-4o-mini': 'OpenAI: GPT-4o-mini',
                'openai/gpt-5-mini': 'OpenAI: GPT-5 Mini',
                'openai/gpt-4.1-mini': 'OpenAI: GPT-4.1 Mini',
                'deepseek/deepseek-v3.2': 'DeepSeek: DeepSeek V3.2',
                'minimax/minimax-m2': 'MiniMax: MiniMax M2',
                'anthropic/claude-sonnet-4': 'Anthropic: Claude Sonnet 4',
                'anthropic/claude-sonnet-4.5': 'Anthropic: Claude Sonnet 4.5',
                'anthropic/claude-opus-4.5': 'Anthropic: Claude Opus 4.5',
                'anthropic/claude-haiku-4.5': 'Anthropic: Claude Haiku 4.5',
                'z-ai/glm-4.6': 'Z.AI: GLM 4.6',
            }
        return jsonify({'code': 1, 'msg': 'success', 'data': {'models': models, 'qdt_cost': {}}})
    except Exception as e:
        logger.error(f"get_public_config failed: {str(e)}")
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/types', methods=['GET'])
def get_market_types():
    """Return supported market types for the add-watchlist modal.

    Visibility is controlled by the centralised :mod:`app.utils.market_visibility`
    rules, primarily the ``ENABLED_MARKETS`` whitelist, with
    ``SHOW_CN_STOCK`` / ``SHOW_HK_STOCK`` kept for back-compat. The radar
    (``/api/global-market/opportunities``) and the Agent API
    (``/api/agent/v1/markets``) read from the same helper so all three
    user-facing market lists stay in lock-step.
    """
    # Keep a stable UX order; CN/HK near US; MOEX last (niche vs crypto/FX/futures).
    desired_order = ['USStock', 'CNStock', 'HKStock', 'Crypto', 'Forex', 'Futures', 'MOEX']
    order_rank = {v: i for i, v in enumerate(desired_order)}

    def _normalize_item(x):
        # Expected: {value: 'USStock', i18nKey: '...'}
        if isinstance(x, dict):
            v = (x.get('value') or '').strip()
            if not v:
                return None
            return {
                'value': v,
                'i18nKey': x.get('i18nKey') or f'dashboard.analysis.market.{v}'
            }
        if isinstance(x, str):
            v = x.strip()
            if not v:
                return None
            return {'value': v, 'i18nKey': f'dashboard.analysis.market.{v}'}
        return None

    def _sort_items(items):
        # Keep unknown market types after known ones, stable by original order.
        out = []
        for it in items or []:
            norm = _normalize_item(it)
            if norm:
                out.append(norm)
        out.sort(key=lambda it: (order_rank.get(it['value'], 10_000)))
        return out

    cfg = load_addon_config()
    data = (cfg.get('market', {}) or {}).get('types')

    # Normalize & force desired order (even if config overrides the list order).
    if isinstance(data, list) and data:
        data = _sort_items(data)
    else:
        data = _sort_items(desired_order)

    data = filter_market_items(data, key='value')
    return jsonify({'code': 1, 'msg': 'success', 'data': data})


@market_blp.route('/menuFooterConfig', methods=['GET'])
def get_menu_footer_config():
    """
    Compatibility stub for old PHP `getMenuFooterConfig`.
    Frontend can also hardcode this locally; this endpoint remains for completeness.
    """
    data = {
        'contact': {
            'support_url': 'https://github.com/',
            'feature_request_url': 'https://github.com/',
            'email': 'support@example.com',
            'live_chat_url': 'https://github.com/'
        },
        'social_accounts': [
            {'name': 'GitHub', 'icon': 'github', 'url': 'https://github.com/'},
            {'name': 'X', 'icon': 'x', 'url': 'https://x.com/'}
        ],
        'legal': {
            'user_agreement': '',
            'privacy_policy': ''
        },
            'copyright': '(c) 2025-2026 QuantDinger'
    }
    return jsonify({'code': 1, 'msg': 'success', 'data': data})

@market_blp.route('/symbols/search', methods=['GET'])
def search_symbols():
    """
    Lightweight symbol search.
    Crypto search is local-only; exchange catalogs are refreshed by the background sync task.
    """
    try:
        market = (request.args.get('market') or '').strip()
        keyword = (request.args.get('keyword') or '').strip().upper()
        limit = int(request.args.get('limit') or 20)
        exchange_id = (request.args.get('exchange_id') or request.args.get('exchangeId') or '').strip()
        market_type = (request.args.get('market_type') or request.args.get('marketType') or '').strip()
        if not market or not keyword:
            return jsonify({'code': 1, 'msg': 'success', 'data': []})

        out = search_market_symbols(
            market=market,
            keyword=keyword,
            limit=limit,
            exchange_id=exchange_id,
            market_type=market_type,
        )
        return jsonify({'code': 1, 'msg': 'success', 'data': out})
    except Exception as e:
        logger.error(f"search_symbols failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500


@market_blp.route('/symbols/hot', methods=['GET'])
def get_hot_symbols():
    """Return a small curated hot list per market (local-only)."""
    try:
        market = (request.args.get('market') or '').strip()
        limit = int(request.args.get('limit') or 10)
        hot = search_hot_symbols(market=market, limit=limit)
        return jsonify({'code': 1, 'msg': 'success', 'data': hot})
    except Exception as e:
        logger.error(f"get_hot_symbols failed: {str(e)}")
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500

@market_blp.route('/watchlist/get', methods=['GET'])
@login_required
def get_watchlist():
    """Get watchlist for the current user."""
    try:
        _ensure_watchlist_table()
        rows = list_watchlist(g.user_id)
        return jsonify({'code': 1, 'msg': 'success', 'data': rows})
    except Exception as e:
        logger.error(f"get_watchlist failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500

@market_blp.route('/watchlist/add', methods=['POST'])
@login_required
def add_watchlist():
    """Add a symbol to watchlist for the current user."""
    try:
        data = request.get_json() or {}
        ok, message = add_watchlist_item(
            g.user_id,
            (data.get('market') or '').strip(),
            normalize_symbol(data.get('symbol')),
            (data.get('name') or '').strip(),
            exchange_id=(data.get('exchange_id') or data.get('exchangeId') or '').strip(),
            market_type=(data.get('market_type') or data.get('marketType') or '').strip(),
            instrument_id=(data.get('instrument_id') or data.get('instrumentId') or '').strip(),
            settle_currency=(data.get('settle_currency') or data.get('settleCurrency') or '').strip(),
        )
        if not ok:
            return jsonify({'code': 0, 'msg': message, 'data': None}), 400
        return jsonify({'code': 1, 'msg': 'success', 'data': None})
    except Exception as e:
        logger.error(f"add_watchlist failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/remove', methods=['POST'])
@login_required
def remove_watchlist():
    """Remove a symbol from watchlist for the current user."""
    try:
        data = request.get_json() or {}
        raw_symbol = normalize_symbol(data.get('symbol'))
        if not raw_symbol:
            return jsonify({'code': 0, 'msg': 'Missing symbol', 'data': None}), 400
        remove_watchlist_item(
            g.user_id,
            (data.get('market') or '').strip(),
            raw_symbol,
            exchange_id=(data.get('exchange_id') or data.get('exchangeId') or '').strip(),
            market_type=(data.get('market_type') or data.get('marketType') or '').strip(),
            instrument_id=(data.get('instrument_id') or data.get('instrumentId') or '').strip(),
        )
        return jsonify({'code': 1, 'msg': 'success', 'data': None})
    except Exception as e:
        logger.error(f"remove_watchlist failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/addBatch', methods=['POST'])
@login_required
def add_watchlist_batch():
    """Batch add watchlist items. Idempotent per item (existing rows update)."""
    try:
        data = request.get_json() or {}
        items = data.get('items')
        if not isinstance(items, list) or not items:
            return jsonify({'code': 0, 'msg': 'items must be a non-empty list', 'data': None}), 400
        if len(items) > 500:
            return jsonify({'code': 0, 'msg': 'At most 500 items per batch', 'data': None}), 400
        group_id = data.get('group_id') if data.get('group_id') is not None else data.get('groupId')
        result = add_watchlist_items(g.user_id, items, group_id=group_id)
        return jsonify({'code': 1, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f"add_watchlist_batch failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/groups', methods=['GET'])
@login_required
def get_watchlist_groups():
    """List the current user's watchlist groups with item counts."""
    try:
        rows = list_watchlist_groups(g.user_id)
        return jsonify({'code': 1, 'msg': 'success', 'data': rows})
    except Exception as e:
        logger.error(f"get_watchlist_groups failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), 500

@market_blp.route('/watchlist/groups', methods=['POST'])
@login_required
def create_watchlist_group_route():
    """Create a watchlist group. Re-creating an existing name returns it."""
    try:
        data = request.get_json() or {}
        ok, message, group = create_watchlist_group(
            g.user_id,
            str(data.get('name') or ''),
            sort_order=int(data.get('sortOrder') or data.get('sort_order') or 0),
        )
        if not ok:
            return jsonify({'code': 0, 'msg': message, 'data': None}), 400
        return jsonify({'code': 1, 'msg': message, 'data': group})
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'msg': 'Invalid sortOrder', 'data': None}), 400
    except Exception as e:
        logger.error(f"create_watchlist_group failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/groups/<int:group_id>', methods=['PUT'])
@login_required
def update_watchlist_group_route(group_id: int):
    """Rename and/or reorder a watchlist group."""
    try:
        data = request.get_json() or {}
        sort_order_raw = data.get('sortOrder', data.get('sort_order'))
        ok, message = update_watchlist_group(
            g.user_id,
            group_id,
            name=str(data['name']) if data.get('name') is not None else None,
            sort_order=int(sort_order_raw) if sort_order_raw is not None else None,
        )
        if not ok:
            status = 404 if message == 'Group not found' else 400
            return jsonify({'code': 0, 'msg': message, 'data': None}), status
        return jsonify({'code': 1, 'msg': message, 'data': None})
    except (TypeError, ValueError):
        return jsonify({'code': 0, 'msg': 'Invalid sortOrder', 'data': None}), 400
    except Exception as e:
        logger.error(f"update_watchlist_group failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/groups/<int:group_id>', methods=['DELETE'])
@login_required
def delete_watchlist_group_route(group_id: int):
    """Delete a group; its items fall back to ungrouped."""
    try:
        deleted = delete_watchlist_group(g.user_id, group_id)
        if not deleted:
            return jsonify({'code': 0, 'msg': 'Group not found', 'data': None}), 404
        return jsonify({'code': 1, 'msg': 'success', 'data': None})
    except Exception as e:
        logger.error(f"delete_watchlist_group failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/move', methods=['POST'])
@login_required
def move_watchlist_items_route():
    """Move items to a group; group_id null moves them to ungrouped."""
    try:
        data = request.get_json() or {}
        item_ids = data.get('itemIds') or data.get('item_ids') or []
        if not isinstance(item_ids, list):
            return jsonify({'code': 0, 'msg': 'itemIds must be a list', 'data': None}), 400
        group_id = data.get('group_id') if data.get('group_id') is not None else data.get('groupId')
        result = move_watchlist_items(g.user_id, item_ids, group_id)
        if result.get('error') and str(result['error']).startswith('Group '):
            return jsonify({'code': 0, 'msg': result['error'], 'data': result}), 404
        return jsonify({'code': 1, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f"move_watchlist_items failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/reorder', methods=['POST'])
@login_required
def reorder_watchlist_route():
    """Persist manual item ordering (list position becomes sort_order)."""
    try:
        data = request.get_json() or {}
        item_ids = data.get('itemIds') or data.get('item_ids') or []
        if not isinstance(item_ids, list):
            return jsonify({'code': 0, 'msg': 'itemIds must be a list', 'data': None}), 400
        result = set_watchlist_order(g.user_id, item_ids)
        if result.get('error'):
            return jsonify({'code': 0, 'msg': result['error'], 'data': result}), 400
        return jsonify({'code': 1, 'msg': 'success', 'data': result})
    except Exception as e:
        logger.error(f"reorder_watchlist failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), 500

@market_blp.route('/watchlist/prices', methods=['GET'])
@login_required
def get_watchlist_prices():
    """Get realtime prices for the current user's watchlist."""
    try:
        user_id = g.user_id
        legacy_param = request.args.get('watchlist')
        if legacy_param:
            try:
                legacy_count = len(json.loads(legacy_param) or [])
            except Exception:
                legacy_count = -1
            logger.debug(
                "watchlist/prices: ignoring legacy client-supplied list (len=%s, user=%s)",
                legacy_count,
                user_id,
            )

        cache_id = cache_key("watchlist_prices", user_id, default_crypto_exchange_id())

        def _compute():
            watchlist = get_user_watchlist_pairs(user_id)
            if not watchlist:
                return []
            results = get_price_map(watchlist, timeout_sec=10)
            success_count = sum(1 for r in results if r.get('price', 0) > 0)
            logger.debug("Watchlist prices: %s/%s successful", success_count, len(results))
            return results

        results = guarded_cached(
            cache_id,
            _compute,
            ttl_sec=5,
            stale_ttl_sec=120,
            timeout_sec=11,
            namespace="watchlist_prices",
            max_concurrent=8,
            cache_if=lambda rows: not rows or all(float(row.get('price') or 0) > 0 for row in rows),
        )
        return jsonify({'code': 1, 'msg': 'success', 'data': results})
    except RequestGuardError as e:
        return jsonify({'code': 0, 'msg': str(e), 'data': []}), e.status_code
    except Exception as e:
        logger.error(f"Batch watchlist price fetch failed: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'code': 0, 'msg': f'Failed: {str(e)}', 'data': []}), 500

@market_blp.route('/price', methods=['GET'])
def get_price():
    """
    Get realtime price for a single symbol.

    Query params:
        market: Market type
        symbol: Symbol or ticker
    """
    try:
        market = (request.args.get('market', '') or '').strip()
        symbol = normalize_symbol(request.args.get('symbol', ''))
        exchange_id = (request.args.get('exchange_id') or request.args.get('exchangeId') or '').strip()
        market_type = (request.args.get('market_type') or request.args.get('marketType') or '').strip()
        instrument_id = (request.args.get('instrument_id') or request.args.get('instrumentId') or '').strip()
        
        if not market or not symbol:
            return jsonify({
                'code': 0,
                'msg': 'Missing market or symbol parameter(s)',
                'data': None
            }), 400

        validation_err = validate_watchlist_pair(market, symbol)
        if validation_err:
            return jsonify({'code': 0, 'msg': validation_err, 'data': None}), 400
        
        result = guarded_cached(
            cache_key("market_price", market, exchange_id, market_type, instrument_id, symbol),
            lambda: get_single_price(market, symbol, exchange_id, market_type, instrument_id),
            ttl_sec=5,
            stale_ttl_sec=120,
            timeout_sec=6,
            namespace="market_price",
            max_concurrent=12,
        )
        
        return jsonify({
            'code': 1,
            'msg': 'success',
            'data': result
        })
    except RequestGuardError as e:
        return jsonify({'code': 0, 'msg': str(e), 'data': None}), e.status_code
        
    except Exception as e:
        logger.error(f"Failed to fetch price: {str(e)}")
        return jsonify({
            'code': 0,
            'msg': f'Failed: {str(e)}',
            'data': None
        }), 500


# openapi-compat: legacy import name
market_bp = market_blp



