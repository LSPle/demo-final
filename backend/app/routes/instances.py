from flask import Blueprint, jsonify, request
from ..models import db, Instance
from ..services.db_validator import db_validator
from ..services.database_service import database_service
from ..services.table_analyzer_service import table_analyzer_service
import pymysql

instances_bp = Blueprint('instances', __name__)

@instances_bp.get('/instances')
def list_instances():
    # 最简方案：按 userId 过滤（query 参数）
    user_id = request.args.get('userId')
    q = Instance.query
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    instances = q.all()
    return jsonify([i.to_dict() for i in instances]), 200

@instances_bp.post('/instances')
def create_instance():
    try:
        data = request.get_json()
        user_id = request.args.get('userId')
        
        if not data:
            return jsonify({'error': '请求体不能为空'}), 400
        
        # 验证必需字段
        required_fields = ['name', 'host', 'port', 'type']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'error': f'缺少必需字段: {field}'}), 400
        
        # 验证字段格式和范围
        if not isinstance(data['name'], str) or len(data['name'].strip()) == 0:
            return jsonify({'error': '实例名称必须是非空字符串'}), 400
        if len(data['name'].strip()) > 100:
            return jsonify({'error': '实例名称长度不能超过100个字符'}), 400
            
        if not isinstance(data['host'], str) or len(data['host'].strip()) == 0:
            return jsonify({'error': '主机地址必须是非空字符串'}), 400
            
        try:
            port = int(data['port'])
            if port < 1 or port > 65535:
                return jsonify({'error': '端口号必须在1-65535范围内'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': '端口号必须是有效的整数'}), 400
            
        if data['type'] not in ['MySQL', 'PostgreSQL', 'Oracle', 'SQL Server']:
            return jsonify({'error': '不支持的数据库类型'}), 400
            

        
        # 检查实例名是否已存在（对该用户范围内）
        base_q = Instance.query
        if user_id is not None:
            base_q = base_q.filter_by(user_id=user_id)
        existing = base_q.filter_by(instance_name=data['name']).first()
        if existing:
            return jsonify({'error': '实例名称已存在'}), 400
        
        # 连接有效性校验（按数据库类型探活）
        is_ok, msg = db_validator.validate_connection(
            db_type=data['type'],
            host=data['host'],
            port=int(data['port']) if 'port' in data else 3306,
            username=data.get('username') or '',
            password=data.get('password') or ''
        )
        if not is_ok:
            return jsonify({'error': f'连接校验失败：{msg}'}), 400
        
        # 创建新实例
        instance = Instance(
            instance_name=data['name'],
            host=data['host'],
            port=int(data['port']) if 'port' in data else 3306,
            username=data.get('username', ''),
            password=data.get('password', ''),
            db_type=data['type'],
            status=data.get('status', 'running'),
            user_id=user_id
        )
        
        db.session.add(instance)
        db.session.commit()
        
        return jsonify({
            'message': '实例创建成功',
            'instance': instance.to_dict()
        }), 201
        
    except ValueError as e:
        return jsonify({'error': f'数据格式错误: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

@instances_bp.put('/instances/<int:instance_id>')
def update_instance(instance_id):
    try:
        user_id = request.args.get('userId')
        q = Instance.query
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        instance = q.filter_by(id=instance_id).first()
        if not instance:
            return jsonify({'error': '实例不存在'}), 404
        data = request.get_json()
        
        if not data:
            return jsonify({'error': '请求体不能为空'}), 400
        
        # 验证字段格式和范围
        if 'name' in data:
            if not isinstance(data['name'], str) or len(data['name'].strip()) == 0:
                return jsonify({'error': '实例名称必须是非空字符串'}), 400
            if len(data['name'].strip()) > 100:
                return jsonify({'error': '实例名称长度不能超过100个字符'}), 400
                
        if 'host' in data:
            if not isinstance(data['host'], str) or len(data['host'].strip()) == 0:
                return jsonify({'error': '主机地址必须是非空字符串'}), 400
                
        if 'port' in data:
            try:
                port = int(data['port'])
                if port < 1 or port > 65535:
                    return jsonify({'error': '端口号必须在1-65535范围内'}), 400
            except (ValueError, TypeError):
                return jsonify({'error': '端口号必须是有效的整数'}), 400
                
        if 'type' in data:
            if data['type'] not in ['MySQL', 'PostgreSQL', 'Oracle', 'SQL Server']:
                return jsonify({'error': '不支持的数据库类型'}), 400
                

        
        # 检查实例名是否与其他实例冲突（用户范围内）
        if 'name' in data and data['name'] != instance.instance_name:
            base_q = Instance.query
            if user_id is not None:
                base_q = base_q.filter_by(user_id=user_id)
            existing = base_q.filter_by(instance_name=data['name']).first()
            if existing:
                return jsonify({'error': '实例名称已存在'}), 400
        
        # 如果更新了连接信息，则重新做连通性校验
        will_check = any(k in data for k in ['host', 'port', 'username', 'password', 'type'])
        if will_check:
            is_ok, msg = db_validator.validate_connection(
                db_type=data.get('type', instance.db_type),
                host=data.get('host', instance.host),
                port=int(data.get('port', instance.port)),
                username=data.get('username', instance.username or ''),
                password=data.get('password', instance.password or '')
            )
            if not is_ok:
                return jsonify({'error': f'连接校验失败：{msg}'}), 400
        
        # 更新字段（移除version）
        if 'name' in data:
            instance.instance_name = data['name']
        if 'host' in data:
            instance.host = data['host']
        if 'port' in data:
            instance.port = int(data['port'])
        if 'username' in data:
            instance.username = data['username']
        if 'password' in data:
            instance.password = data['password']
        if 'type' in data:
            instance.db_type = data['type']
        if 'status' in data:
            instance.status = data['status']

        
        db.session.commit()
        
        return jsonify({
            'message': '实例更新成功',
            'instance': instance.to_dict()
        }), 200
        
    except ValueError as e:
        return jsonify({'error': f'数据格式错误: {str(e)}'}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

@instances_bp.delete('/instances/<int:instance_id>')
def delete_instance(instance_id):
    try:
        user_id = request.args.get('userId')
        q = Instance.query
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        instance = q.filter_by(id=instance_id).first()
        if not instance:
            return jsonify({'error': '实例不存在'}), 404
        instance_name = instance.instance_name
        
        db.session.delete(instance)
        db.session.commit()
        
        return jsonify({
            'message': f'实例 "{instance_name}" 删除成功'
        }), 200
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

@instances_bp.get('/instances/<int:instance_id>')
def get_instance(instance_id):
    try:
        user_id = request.args.get('userId')
        q = Instance.query
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        instance = q.filter_by(id=instance_id).first()
        if not instance:
            return jsonify({'error': '实例不存在'}), 404
        return jsonify(instance.to_dict()), 200
    except Exception as e:
        return jsonify({'error': f'服务器错误: {str(e)}'}), 500

@instances_bp.get('/instances/<int:instance_id>/databases')
def list_instance_databases(instance_id):
    try:
        user_id = request.args.get('userId')
        q = Instance.query
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        inst = q.filter_by(id=instance_id).first()
        if not inst:
            return jsonify({'error': '实例不存在'}), 404
        
        ok, dbs, msg = database_service.list_databases(inst)
        if not ok:
            # 如果是类型不支持，按 400 返回；否则按 500/连接错误可 400
            if msg == '仅支持MySQL实例':
                return jsonify({'error': msg}), 400
            if msg == 'MySQL驱动不可用':
                return jsonify({'error': msg}), 500
            return jsonify({'error': msg}), 400
        
        return jsonify({'databases': dbs}), 200
    except Exception as e:
        return jsonify({'error': f'服务器错误: {e}'}), 500


@instances_bp.get('/instances/<int:instance_id>/databases/<string:database>/tables')
def list_tables(instance_id, database):
    try:
        user_id = request.args.get('userId')
        q = Instance.query
        if user_id is not None:
            q = q.filter_by(user_id=user_id)
        inst = q.filter_by(id=instance_id).first()
        if not inst:
            return jsonify({'error': '实例不存在'}), 404
        if (inst.db_type or '').strip() != 'MySQL':
            return jsonify({'error': '仅支持MySQL实例'}), 400
        if not pymysql:
            return jsonify({'error': 'MySQL驱动不可用'}), 500

        conn = pymysql.connect(
            host=inst.host,
            port=inst.port,
            user=inst.username or '',
            password=inst.password or '',
            database=database,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                rows = cursor.fetchall()
                # 兼容不同返回键名（如 'Tables_in_dbname'）
                tables = []
                for row in rows:
                    if isinstance(row, dict) and row:
                        tables.append(list(row.values())[0])
                    elif isinstance(row, (list, tuple)) and row:
                        tables.append(row[0])
                tables.sort()
            return jsonify({'tables': tables}), 200
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': f'获取数据表失败: {e}'}), 500


@instances_bp.get('/instances/<int:instance_id>/databases/<string:database>/tables/<string:table_name>/schema')
def get_table_schema(instance_id, database, table_name):
    try:
        inst = Instance.query.get(instance_id)
        if not inst:
            return jsonify({'error': '实例不存在'}), 404
        if (inst.db_type or '').strip() != 'MySQL':
            return jsonify({'error': '仅支持MySQL实例'}), 400

        ok, meta, msg = table_analyzer_service._get_table_metadata_only(inst, database, table_name)
        if not ok:
            # 返回详细错误信息
            return jsonify({'error': msg}), 400
        return jsonify({'schema': meta}), 200
    except Exception as e:
        return jsonify({'error': f'获取表结构失败: {e}'}), 500