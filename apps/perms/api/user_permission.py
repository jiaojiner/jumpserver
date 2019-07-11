# -*- coding: utf-8 -*-
#
import time
import traceback
import uuid
from hashlib import md5
from django.core.cache import cache
from django.conf import settings
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView, Response
from rest_framework.generics import (
    ListAPIView, get_object_or_404, RetrieveAPIView
)
from django.utils.translation import ugettext as _
from rest_framework.pagination import LimitOffsetPagination

from common.permissions import IsValidUser, IsOrgAdminOrAppUser
from common.tree import TreeNodeSerializer
from common.utils import get_logger, get_object_or_none
from ..utils import (
    AssetPermissionUtil, ParserNode,
)
from .. import const
from ..hands import User, Asset, Node, SystemUser, NodeSerializer
from .. import serializers
from ..models import Action


logger = get_logger(__name__)

__all__ = [
    'UserGrantedAssetsApi', 'UserGrantedNodesApi',
    'UserGrantedNodesWithAssetsApi', 'UserGrantedNodeAssetsApi',
    'ValidateUserAssetPermissionApi', 'UserGrantedNodesAsTreeApi',
    'UserGrantedNodesWithAssetsAsTreeApi', 'GetUserAssetPermissionActionsApi',
]


class UserPermissionCacheMixin:
    cache_policy = '0'
    RESP_CACHE_KEY = '_PERMISSION_RESPONSE_CACHE_V2_{}'
    CACHE_TIME = settings.ASSETS_PERM_CACHE_TIME
    _object = None

    def get_object(self):
        return None

    # 内部使用可控制缓存
    def _get_object(self):
        if not self._object:
            self._object = self.get_object()
        return self._object

    def get_object_id(self):
        obj = self._get_object()
        if obj:
            return str(obj.id)
        return None

    def get_request_md5(self):
        path = self.request.path
        query = {k: v for k, v in self.request.GET.items()}
        query.pop("_", None)
        query = "&".join(["{}={}".format(k, v) for k, v in query.items()])
        full_path = "{}?{}".format(path, query)
        return md5(full_path.encode()).hexdigest()

    def get_meta_cache_id(self):
        obj = self._get_object()
        util = AssetPermissionUtil(obj, cache_policy=self.cache_policy)
        meta_cache_id = util.cache_meta.get('id')
        return meta_cache_id

    def get_response_cache_id(self):
        obj_id = self.get_object_id()
        request_md5 = self.get_request_md5()
        meta_cache_id = self.get_meta_cache_id()
        resp_cache_id = '{}_{}_{}'.format(obj_id, request_md5, meta_cache_id)
        return resp_cache_id

    def get_response_from_cache(self):
        # 没有数据缓冲
        meta_cache_id = self.get_meta_cache_id()
        if not meta_cache_id:
            logger.debug("Not get meta id: {}".format(meta_cache_id))
            return None
        # 从响应缓冲里获取响应
        key = self.get_response_key()
        data = cache.get(key)
        if not data:
            logger.debug("Not get response from cache: {}".format(key))
            return None
        logger.debug("Get user permission from cache: {}".format(self.get_object()))
        response = Response(data)
        return response

    def expire_response_cache(self):
        obj_id = self.get_object_id()
        expire_cache_id = '{}_{}'.format(obj_id, '*')
        key = self.RESP_CACHE_KEY.format(expire_cache_id)
        cache.delete_pattern(key)

    def get_response_key(self):
        resp_cache_id = self.get_response_cache_id()
        key = self.RESP_CACHE_KEY.format(resp_cache_id)
        return key

    def set_response_to_cache(self, response):
        key = self.get_response_key()
        cache.set(key, response.data, self.CACHE_TIME)
        logger.debug("Set response to cache: {}".format(key))

    def get(self, request, *args, **kwargs):
        self.cache_policy = request.GET.get('cache_policy', '0')

        obj = self._get_object()
        if obj is None:
            logger.debug("Not get response from cache: obj is none")
            return super().get(request, *args, **kwargs)

        if AssetPermissionUtil.is_not_using_cache(self.cache_policy):
            logger.debug("Not get resp from cache: {}".format(self.cache_policy))
            return super().get(request, *args, **kwargs)
        elif AssetPermissionUtil.is_refresh_cache(self.cache_policy):
            logger.debug("Not get resp from cache: {}".format(self.cache_policy))
            self.expire_response_cache()

        logger.debug("Try get response from cache")
        resp = self.get_response_from_cache()
        if not resp:
            resp = super().get(request, *args, **kwargs)
            self.set_response_to_cache(resp)
        return resp


class GrantAssetsMixin:
    serializer_class = serializers.AssetGrantedSerializer

    def get_serializer(self, queryset, many=True):
        assets_ids = []
        system_users_ids = set()
        for asset in queryset:
            assets_ids.append(asset["id"])
            system_users_ids.update(set(asset["system_users"]))
        assets = Asset.objects.filter(id__in=assets_ids).only(
            *self.serializer_class.Meta.only_fields
        )
        assets_map = {asset.id: asset for asset in assets}
        system_users = SystemUser.objects.filter(id__in=system_users_ids).only(
            *self.serializer_class.system_users_only_fields
        )
        system_users_map = {s.id: s for s in system_users}
        data = []
        for item in queryset:
            i = item["id"]
            asset = assets_map.get(i)
            if not asset:
                continue

            _system_users = item["system_users"]
            system_users_granted = []
            for sid, action in _system_users.items():
                system_user = system_users_map.get(sid)
                if not system_user:
                    continue
                system_user.actions = action
                system_users_granted.append(system_user)
            asset.system_users_granted = system_users_granted
            data.append(asset)
        return super().get_serializer(data, many=True)

    def search_queryset(self, assets):
        search = self.request.query_params.get("search")
        if not search:
            return assets

        assets_map = {asset['id']: asset for asset in assets}
        assets_ids = set(assets_map.keys())
        assets_ids_search = Asset.objects.filter(id__in=assets_ids).filter(
            Q(hostname__icontains=search) | Q(ip__icontains=search)
        ).values_list('id', flat=True)
        assets_ids &= set(assets_ids_search)
        return [assets_map.get(asset_id) for asset_id in assets_ids]


class UserGrantedAssetsApi(UserPermissionCacheMixin, GrantAssetsMixin, ListAPIView):
    """
    用户授权的所有资产
    """
    permission_classes = (IsOrgAdminOrAppUser,)
    pagination_class = LimitOffsetPagination

    def get_object(self):
        user_id = self.kwargs.get('pk', '')
        if user_id:
            user = get_object_or_404(User, id=user_id)
        else:
            user = self.request.user
        return user

    def get_queryset(self):
        user = self.get_object()
        util = AssetPermissionUtil(user, cache_policy=self.cache_policy)
        queryset = util.get_assets()
        queryset = self.search_queryset(queryset)
        return queryset

    def get_permissions(self):
        if self.kwargs.get('pk') is None:
            self.permission_classes = (IsValidUser,)
        return super().get_permissions()


class NodesWithUngroupMixin:
    util = None

    @staticmethod
    def get_ungrouped_node(ungroup_key):
        return Node(key=ungroup_key, id=const.UNGROUPED_NODE_ID,
                    value=_("ungrouped"))

    @staticmethod
    def get_empty_node():
        return Node(key=const.EMPTY_NODE_KEY, id=const.EMPTY_NODE_ID,
                    value=_("empty"))

    def add_ungrouped_nodes(self, node_map, node_keys):
        ungroup_key = '1:-1'
        for key in node_keys:
            if key.endswith('-1'):
                ungroup_key = key
                break
        ungroup_node = self.get_ungrouped_node(ungroup_key)
        empty_node = self.get_empty_node()
        node_map[ungroup_key] = ungroup_node
        node_map[const.EMPTY_NODE_KEY] = empty_node


class UserGrantedNodesApi(UserPermissionCacheMixin, NodesWithUngroupMixin, ListAPIView):
    """
    查询用户授权的所有节点的API
    """
    permission_classes = (IsOrgAdminOrAppUser,)
    serializer_class = NodeSerializer
    pagination_class = LimitOffsetPagination
    only_fields = NodeSerializer.Meta.only_fields

    def get_object(self):
        user_id = self.kwargs.get('pk', '')
        if user_id:
            user = get_object_or_404(User, id=user_id)
        else:
            user = self.request.user
        return user

    def get_nodes(self, nodes_with_assets):
        node_keys = [n["key"] for n in nodes_with_assets]
        nodes = Node.objects.filter(key__in=node_keys).only(
            *self.only_fields
        )
        nodes_map = {n.key: n for n in nodes}
        self.add_ungrouped_nodes(nodes_map, node_keys)

        _nodes = []
        for n in nodes_with_assets:
            key = n["key"]
            node = nodes_map.get(key)
            node._assets_amount = n["assets_amount"]
            _nodes.append(node)
        return _nodes

    def get_serializer(self, nodes_with_assets, many=True):
        nodes = self.get_nodes(nodes_with_assets)
        return super().get_serializer(nodes, many=True)

    def get_queryset(self):
        user = self.get_object()
        self.util = AssetPermissionUtil(user, cache_policy=self.cache_policy)
        nodes_with_assets = self.util.get_nodes_with_assets()
        return nodes_with_assets

    def get_permissions(self):
        if self.kwargs.get('pk') is None:
            self.permission_classes = (IsValidUser,)
        return super().get_permissions()


class UserGrantedNodesAsTreeApi(UserGrantedNodesApi):
    serializer_class = TreeNodeSerializer
    only_fields = ParserNode.nodes_only_fields

    def get_serializer(self, nodes_with_assets, many=True):
        nodes = self.get_nodes(nodes_with_assets)
        queryset = []
        for node in nodes:
            data = ParserNode.parse_node_to_tree_node(node)
            queryset.append(data)
        return self.get_serializer_class()(queryset, many=many)


class UserGrantedNodesWithAssetsApi(UserPermissionCacheMixin, NodesWithUngroupMixin, ListAPIView):
    """
    用户授权的节点并带着节点下资产的api
    """
    permission_classes = (IsOrgAdminOrAppUser,)
    serializer_class = serializers.NodeGrantedSerializer
    pagination_class = LimitOffsetPagination

    nodes_only_fields = serializers.NodeGrantedSerializer.Meta.only_fields
    assets_only_fields = serializers.NodeGrantedSerializer.assets_only_fields
    system_users_only_fields = serializers.NodeGrantedSerializer.system_users_only_fields

    def get_object(self):
        user_id = self.kwargs.get('pk', '')
        if not user_id:
            user = self.request.user
        else:
            user = get_object_or_404(User, id=user_id)
        return user

    def get_maps(self, nodes_items):
        """
        查库，并加入构造的ungrouped节点
        :return:
        ({asset.id: asset}, {node.key: node}, {system_user.id: system_user})
        """
        _nodes_keys = set()
        _assets_ids = set()
        _system_users_ids = set()
        for item in nodes_items:
            _nodes_keys.add(item["key"])
            _assets_ids.update(set(item["assets"].keys()))
            for _system_users_id in item["assets"].values():
                _system_users_ids.update(_system_users_id.keys())

        _nodes = Node.objects.filter(key__in=_nodes_keys).only(
            *self.nodes_only_fields
        )
        _assets = Asset.objects.filter(id__in=_assets_ids).only(
            *self.assets_only_fields
        )
        _system_users = SystemUser.objects.filter(id__in=_system_users_ids).only(
            *self.system_users_only_fields
        )
        _nodes_map = {n.key: n for n in _nodes}
        self.add_ungrouped_nodes(_nodes_map, _nodes_keys)
        _assets_map = {a.id: a for a in _assets}
        _system_users_map = {s.id: s for s in _system_users}
        return _nodes_map, _assets_map, _system_users_map

    def get_serializer_queryset(self, nodes_items):
        """
        将id转为object，同时构造queryset
        :param nodes_items:
        [
            {
                'key': node.key,
                'assets_amount': 10
                'assets': {
                    asset.id: {
                        system_user.id: actions,
                    },
                },
            },
        ]
        """
        queryset = []
        _node_map, _assets_map, _system_users_map = self.get_maps(nodes_items)
        for item in nodes_items:
            key = item["key"]
            node = _node_map.get(key)
            if not node:
                continue
            node._assets_amount = item["assets_amount"]
            assets_granted = []
            for asset_id, system_users_ids_action in item["assets"].items():
                asset = _assets_map.get(asset_id)
                if not asset:
                    continue
                system_user_granted = []
                for system_user_id, action in system_users_ids_action.items():
                    system_user = _system_users_map.get(system_user_id)
                    if not system_user:
                        continue
                    system_user.actions = action
                    system_user_granted.append(system_user)
                asset.system_users_granted = system_user_granted
                assets_granted.append(asset)
            node.assets_granted = assets_granted
            queryset.append(node)
        return queryset

    def get_serializer(self, nodes_items, many=True):
        queryset = self.get_serializer_queryset(nodes_items)
        return super().get_serializer(queryset, many=many)

    def get_queryset(self):
        user = self.get_object()
        self.util = AssetPermissionUtil(user, cache_policy=self.cache_policy)
        system_user_id = self.request.query_params.get('system_user')
        if system_user_id:
            self.util.filter_permissions(
                system_users=system_user_id
            )
        nodes_items = self.util.get_nodes_with_assets()
        return nodes_items

    def get_permissions(self):
        if self.kwargs.get('pk') is None:
            self.permission_classes = (IsValidUser,)
        return super().get_permissions()


class UserGrantedNodesWithAssetsAsTreeApi(UserGrantedNodesWithAssetsApi):
    serializer_class = TreeNodeSerializer
    permission_classes = (IsOrgAdminOrAppUser,)
    system_user_id = None
    nodes_only_fields = ParserNode.nodes_only_fields
    assets_only_fields = ParserNode.assets_only_fields
    system_users_only_fields = ParserNode.system_users_only_fields

    def get_serializer(self, nodes_items, many=True):
        _queryset = super().get_serializer_queryset(nodes_items)
        queryset = []

        for node in _queryset:
            data = ParserNode.parse_node_to_tree_node(node)
            queryset.append(data)
            for asset in node.assets_granted:
                system_users = asset.system_users_granted
                data = ParserNode.parse_asset_to_tree_node(node, asset, system_users)
                queryset.append(data)
        queryset = sorted(queryset)
        return self.serializer_class(queryset, many=True)


class UserGrantedNodeAssetsApi(UserPermissionCacheMixin, GrantAssetsMixin, ListAPIView):
    """
    查询用户授权的节点下的资产的api, 与上面api不同的是，只返回某个节点下的资产
    """
    permission_classes = (IsOrgAdminOrAppUser,)
    pagination_class = LimitOffsetPagination

    def get_object(self):
        user_id = self.kwargs.get('pk', '')

        if user_id:
            user = get_object_or_404(User, id=user_id)
        else:
            user = self.request.user
        return user

    def get_node_key(self):
        node_id = self.kwargs.get('node_id')
        if str(node_id) == const.UNGROUPED_NODE_ID:
            key = self.util.tree.ungrouped_key
        elif str(node_id) == const.EMPTY_NODE_ID:
            key = const.EMPTY_NODE_KEY
        else:
            node = get_object_or_404(Node, id=node_id)
            key = node.key
        return key

    def get_queryset(self):
        user = self.get_object()
        self.util = AssetPermissionUtil(user, cache_policy=self.cache_policy)
        key = self.get_node_key()
        nodes_items = self.util.get_nodes_with_assets()
        assets_system_users = {}
        for item in nodes_items:
            if item["key"] == key:
                assets_system_users = item["assets"]
                break
        assets = []
        for asset_id, system_users in assets_system_users.items():
            assets.append({"id": asset_id, "system_users": system_users})
        assets = self.search_queryset(assets)
        return assets

    def get_permissions(self):
        if self.kwargs.get('pk') is None:
            self.permission_classes = (IsValidUser,)
        return super().get_permissions()


class ValidateUserAssetPermissionApi(UserPermissionCacheMixin, APIView):
    permission_classes = (IsOrgAdminOrAppUser,)
    
    def get(self, request, *args, **kwargs):
        user_id = request.query_params.get('user_id', '')
        asset_id = request.query_params.get('asset_id', '')
        system_id = request.query_params.get('system_user_id', '')
        action_name = request.query_params.get('action_name', '')
        cache_policy = self.request.query_params.get("cache_policy", '0')

        try:
            asset_id = uuid.UUID(asset_id)
            system_id = uuid.UUID(system_id)
        except ValueError:
            return Response({'msg': False}, status=403)

        user = get_object_or_404(User, id=user_id)
        util = AssetPermissionUtil(user, cache_policy=cache_policy)
        assets = util.get_assets()
        for asset in assets:
            if asset_id == asset["id"]:
                action = asset["system_users"].get(system_id)
                if action and action_name in Action.value_to_choices(action):
                    return Response({'msg': True}, status=200)
                break
        return Response({'msg': False}, status=403)


class GetUserAssetPermissionActionsApi(UserPermissionCacheMixin, RetrieveAPIView):
    permission_classes = (IsOrgAdminOrAppUser,)
    serializer_class = serializers.ActionsSerializer

    def get_object(self):
        user_id = self.request.query_params.get('user_id', '')
        asset_id = self.request.query_params.get('asset_id', '')
        system_id = self.request.query_params.get('system_user_id', '')

        user = get_object_or_404(User, id=user_id)
        asset = get_object_or_404(Asset, id=asset_id)
        su = get_object_or_404(SystemUser, id=system_id)

        util = AssetPermissionUtil(user, cache_policy=self.cache_policy)
        granted_assets = util.get_assets()
        granted_system_users = granted_assets.get(asset, {})

        _object = {}
        if su not in granted_system_users:
            _object['actions'] = 0
        else:
            _object['actions'] = granted_system_users[su]
        return _object
