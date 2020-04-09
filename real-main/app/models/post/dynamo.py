from collections import defaultdict
from functools import reduce
import logging

from boto3.dynamodb.conditions import Attr, Key
import pendulum

from app.models.like.enums import LikeStatus
from app.models.post.enums import PostStatus

from . import exceptions, enums

logger = logging.getLogger()


class PostDynamo:

    def __init__(self, dynamo_client):
        self.client = dynamo_client

    def get_post(self, post_id, strongly_consistent=False):
        return self.client.get_item({
            'partitionKey': f'post/{post_id}',
            'sortKey': '-',
        }, strongly_consistent=strongly_consistent)

    def get_original_metadata(self, post_id):
        return self.client.get_item({
            'partitionKey': f'post/{post_id}',
            'sortKey': 'originalMetadata',
        })

    def delete_post(self, post_id):
        query_kwargs = {'Key': {
            'partitionKey': f'post/{post_id}',
            'sortKey': '-',
        }}
        return self.client.delete_item(query_kwargs)

    def delete_original_metadata(self, post_id):
        query_kwargs = {'Key': {
            'partitionKey': f'post/{post_id}',
            'sortKey': 'originalMetadata',
        }}
        return self.client.delete_item(query_kwargs)

    def get_next_completed_post_to_expire(self, user_id, exclude_post_id=None):
        query_kwargs = {
            'KeyConditionExpression': (
                Key('gsiA1PartitionKey').eq(f'post/{user_id}')
                & Key('gsiA1SortKey').begins_with(f'{PostStatus.COMPLETED}/')
            ),
            'IndexName': 'GSI-A1',
        }
        if exclude_post_id:
            query_kwargs['FilterExpression'] = Attr('postId').ne(exclude_post_id)
        return next(self.client.generate_all_query(query_kwargs), None)

    def batch_get_posted_by_user_ids(self, post_ids):
        "Given a list of post_ids, return a dict of post_id -> posted_by_user_id"
        projection_expression = 'postedByUserId'
        typed_keys = [{
            'partitionKey': {'S': f'post/{post_id}'},
            'sortKey': {'S': '-'}
        } for post_id in post_ids]
        typed_result = self.client.batch_get_items(typed_keys, projection_expression)
        return [r['postedByUserId']['S'] for r in typed_result]

    def generate_posts_by_user(self, user_id, completed=None):
        query_kwargs = {
            'KeyConditionExpression': Key('gsiA2PartitionKey').eq(f'post/{user_id}'),
            'IndexName': 'GSI-A2',
        }
        if completed is not None:
            comparison = '=' if completed else '<>'
            query_kwargs['FilterExpression'] = f'postStatus {comparison} :status'
            query_kwargs['ExpressionAttributeValues'] = {':status': PostStatus.COMPLETED}
        return self.client.generate_all_query(query_kwargs)

    def generate_expired_post_pks_by_day(self, date, cut_off_time=None):
        key_conditions = [Key('gsiK1PartitionKey').eq(f'post/{date}')]
        if cut_off_time:
            key_conditions.append(Key('gsiK1SortKey').lt(str(cut_off_time)))
        query_kwargs = {
            'KeyConditionExpression': reduce(lambda a, b: a & b, key_conditions),
            'IndexName': 'GSI-K1',
            'ProjectionExpression': 'partitionKey, sortKey',
        }
        return self.client.generate_all_query(query_kwargs)

    def generate_expired_post_pks_with_scan(self, cut_off_date):
        "Do a table **scan** to generate pks of expired posts. Does *not* include cut_off_date."
        query_kwargs = {
            'FilterExpression': (
                Attr('partitionKey').begins_with('post/')
                & Attr('expiresAt').lt(str(cut_off_date))
            ),
            'ProjectionExpression': 'partitionKey, sortKey',
        }
        return self.client.generate_all_scan(query_kwargs)

    def transact_add_pending_post(self, posted_by_user_id, post_id, post_type, posted_at=None, expires_at=None,
                                  album_id=None, text=None, text_tags=None, comments_disabled=None,
                                  likes_disabled=None, sharing_disabled=None, verification_hidden=None):
        posted_at = posted_at or pendulum.now('utc')
        posted_at_str = posted_at.to_iso8601_string()
        post_status = enums.PostStatus.PENDING
        post_item = {
            'schemaVersion': {'N': '3'},
            'partitionKey': {'S': f'post/{post_id}'},
            'sortKey': {'S': '-'},
            'gsiA2PartitionKey': {'S': f'post/{posted_by_user_id}'},
            'gsiA2SortKey': {'S': f'{post_status}/{posted_at_str}'},
            'gsiA3PartitionKey': {'S': f'post/{posted_by_user_id}'},
            'gsiA3SortKey': {'S': f'{post_status}/{post_type}/{posted_at_str}'},
            'postId': {'S': post_id},
            'postedAt': {'S': posted_at_str},
            'postedByUserId': {'S': posted_by_user_id},
            'postType': {'S': post_type},
            'postStatus': {'S': post_status},
        }
        if expires_at:
            expires_at_str = expires_at.to_iso8601_string()
            post_item.update({
                'expiresAt': {'S': expires_at_str},
                'gsiA1PartitionKey': {'S': f'post/{posted_by_user_id}'},
                'gsiA1SortKey': {'S': f'{post_status}/{expires_at_str}'},
                'gsiK1PartitionKey': {'S': f'post/{expires_at.date()}'},
                'gsiK1SortKey': {'S': str(expires_at.time())},
            })
        if album_id:
            post_item.update({
                'albumId': {'S': album_id},
                'gsiK3PartitionKey': {'S': f'post/{album_id}'},
                'gsiK3SortKey': {'N': '-1'},  # all non-completed posts have a rank of -1
            })
        if text:
            post_item['text'] = {'S': text}
        if text_tags is not None:
            post_item['textTags'] = {'L': [
                {'M': {
                    'tag': {'S': text_tag['tag']},
                    'userId': {'S': text_tag['userId']},
                }}
                for text_tag in text_tags
            ]}
        if comments_disabled is not None:
            post_item['commentsDisabled'] = {'BOOL': comments_disabled}
        if likes_disabled is not None:
            post_item['likesDisabled'] = {'BOOL': likes_disabled}
        if sharing_disabled is not None:
            post_item['sharingDisabled'] = {'BOOL': sharing_disabled}
        if verification_hidden is not None:
            post_item['verificationHidden'] = {'BOOL': verification_hidden}

        return {'Put': {
            'Item': post_item,
            'ConditionExpression': 'attribute_not_exists(partitionKey)',  # no updates, just adds
        }}

    def transact_add_original_metadata(self, post_id, original_metadata):
        return {'Put': {
            'Item': {
                'schemaVersion': {'N': '0'},
                'partitionKey': {'S': f'post/{post_id}'},
                'sortKey': {'S': 'originalMetadata'},
                'originalMetadata': {'S': original_metadata},
            },
            'ConditionExpression': 'attribute_not_exists(partitionKey)',  # no updates, just adds
        }}

    def transact_increment_flag_count(self, post_id):
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD flagCount :one',
                'ExpressionAttributeValues': {
                    ':one': {'N': '1'},
                },
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            }
        }

    def transact_decrement_flag_count(self, post_id):
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD flagCount :neg_one',
                'ExpressionAttributeValues': {
                    ':neg_one': {'N': '-1'},
                    ':zero': {'N': '0'},
                },
                'ConditionExpression': 'attribute_exists(partitionKey) AND flagCount > :zero',
            }
        }

    def transact_set_post_status(self, post_item, status, original_post_id=None, album_rank=None):
        album_id = post_item.get('albumId')

        assert (album_rank is not None) is bool(album_id and status == PostStatus.COMPLETED), \
            'album_rank must be specified only when completing a post in an album'
        album_rank = album_rank if album_rank is not None else -1

        exp_sets = ['postStatus = :postStatus', 'gsiA2SortKey = :gsia2sk', 'gsiA3SortKey = :gsia3sk']
        exp_values = {
            ':postStatus': {'S': status},
            ':gsia2sk': {'S': f'{status}/{post_item["postedAt"]}'},
            ':gsia3sk': {'S': f'{status}/{post_item["postType"]}/{post_item["postedAt"]}'},
        }

        if original_post_id:
            exp_sets.append('originalPostId = :opi')
            exp_values[':opi'] = {'S': original_post_id}

        if album_id:
            exp_sets.append('gsiK3SortKey = :ar')
            exp_values[':ar'] = {'N': str(album_rank)}

        if 'expiresAt' in post_item:
            exp_sets.append('gsiA1SortKey = :gsiA1SortKey')
            exp_values[':gsiA1SortKey'] = {'S': f'{status}/{post_item["expiresAt"]}'}

        transact = {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_item["postId"]}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'SET ' + ', '.join(exp_sets),
                'ExpressionAttributeValues': exp_values,
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            },
        }
        return transact

    def increment_viewed_by_count(self, post_id):
        query_kwargs = {
            'Key': {
                'partitionKey': f'post/{post_id}',
                'sortKey': '-',
            },
            'UpdateExpression': 'ADD viewedByCount :one',
            'ExpressionAttributeValues': {':one': 1},
        }
        try:
            return self.client.update_item(query_kwargs)
        except self.client.exceptions.ConditionalCheckFailedException:
            raise exceptions.PostDoesNotExist(post_id)

    def set(self, post_id, text=None, text_tags=None, comments_disabled=None, likes_disabled=None,
            sharing_disabled=None, verification_hidden=None):
        assert any(k is not None for k in (
            text, comments_disabled, likes_disabled, sharing_disabled, verification_hidden,
        )), 'Action-less post edit requested'

        exp_actions = defaultdict(list)
        exp_names = {}
        exp_values = {}

        if text is not None:
            # empty string deletes
            if text == '':
                exp_actions['REMOVE'].append('#text')
                exp_actions['REMOVE'].append('textTags')
                exp_names['#text'] = 'text'
            else:
                exp_actions['SET'].append('#text = :text')
                exp_names['#text'] = 'text'
                exp_values[':text'] = text

                if text_tags is not None:
                    exp_actions['SET'].append('textTags = :tu')
                    exp_values[':tu'] = text_tags

        if comments_disabled is not None:
            exp_actions['SET'].append('commentsDisabled = :cd')
            exp_values[':cd'] = comments_disabled

        if likes_disabled is not None:
            exp_actions['SET'].append('likesDisabled = :ld')
            exp_values[':ld'] = likes_disabled

        if sharing_disabled is not None:
            exp_actions['SET'].append('sharingDisabled = :sd')
            exp_values[':sd'] = sharing_disabled

        if verification_hidden is not None:
            exp_actions['SET'].append('verificationHidden = :vd')
            exp_values[':vd'] = verification_hidden

        update_query_kwargs = {
            'Key': {
                'partitionKey': f'post/{post_id}',
                'sortKey': '-',
            },
            'UpdateExpression': ' '.join([f'{k} {", ".join(v)}' for k, v in exp_actions.items()]),
        }

        if exp_names:
            update_query_kwargs['ExpressionAttributeNames'] = exp_names
        if exp_values:
            update_query_kwargs['ExpressionAttributeValues'] = exp_values

        return self.client.update_item(update_query_kwargs)

    def set_checksum(self, post_id, posted_at_str, checksum):
        assert checksum  # no deletes
        query_kwargs = {
            'Key': {
                'partitionKey': f'post/{post_id}',
                'sortKey': '-',
            },
            'UpdateExpression': 'SET checksum = :checksum, gsiK2PartitionKey = :pk, gsiK2SortKey = :sk',
            'ExpressionAttributeValues': {
                ':checksum': checksum,
                ':pk': f'postChecksum/{checksum}',
                ':sk': posted_at_str,
            },
        }
        return self.client.update_item(query_kwargs)

    def get_first_with_checksum(self, checksum):
        query_kwargs = {
            'KeyConditionExpression': Key('gsiK2PartitionKey').eq(f'postChecksum/{checksum}'),
            'IndexName': 'GSI-K2',
        }
        keys = self.client.query_head(query_kwargs)
        post_id = keys['partitionKey'].split('/')[1] if keys else None
        posted_at = keys['gsiK2SortKey'] if keys else None
        return post_id, posted_at

    def transact_set_has_new_comment_activity(self, post_id, new_value):
        """
        Set the boolean Post.hasNewCommentActivity.
        If the post already had the value that we're seting to, an exception will be thrown.
        """
        cond_exp = 'attribute_exists(partitionKey)'
        if new_value:
            cond_exp += ' AND (attribute_not_exists(hasNewCommentActivity) OR hasNewCommentActivity = :ov)'
        else:
            cond_exp += ' AND hasNewCommentActivity = :ov'
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'SET hasNewCommentActivity = :nv',
                'ExpressionAttributeValues': {
                    ':nv': {'BOOL': new_value},
                    ':ov': {'BOOL': not new_value},
                },
                'ConditionExpression': cond_exp,
            },
        }

    def set_expires_at(self, post_item, expires_at):
        expires_at_str = expires_at.to_iso8601_string()
        update_query_kwargs = {
            'Key': {
                'partitionKey': f'post/{post_item["postId"]}',
                'sortKey': '-',
            },
            'UpdateExpression': 'SET ' + ', '.join([
                'expiresAt = :ea',
                'gsiA1PartitionKey = :ga1pk',
                'gsiA1SortKey = :ga1sk',
                'gsiK1PartitionKey = :gk1pk',
                'gsiK1SortKey = :gk1sk',
            ]),
            'ExpressionAttributeValues': {
                ':ea': expires_at_str,
                ':ga1pk': 'post/' + post_item['postedByUserId'],
                ':ga1sk': post_item['postStatus'] + '/' + expires_at_str,
                ':gk1pk': f'post/{expires_at.date()}',
                ':gk1sk': str(expires_at.time()),
                ':ps': post_item['postStatus'],
            },
            'ConditionExpression': 'postStatus = :ps',
        }
        return self.client.update_item(update_query_kwargs)

    def remove_expires_at(self, post_id):
        update_query_kwargs = {
            'Key': {
                'partitionKey': f'post/{post_id}',
                'sortKey': '-',
            },
            'UpdateExpression': 'REMOVE expiresAt, gsiA1PartitionKey, gsiA1SortKey, gsiK1PartitionKey, gsiK1SortKey',
        }
        return self.client.update_item(update_query_kwargs)

    def transact_increment_like_count(self, post_id, like_status):
        if like_status == LikeStatus.ONYMOUSLY_LIKED:
            like_count_attribute = 'onymousLikeCount'
        elif like_status == LikeStatus.ANONYMOUSLY_LIKED:
            like_count_attribute = 'anonymousLikeCount'
        else:
            raise exceptions.PostException(f'Unrecognized like status `{like_status}`')

        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD #count_name :one',
                'ExpressionAttributeValues': {
                    ':one': {'N': '1'},
                },
                'ExpressionAttributeNames': {
                    '#count_name': like_count_attribute,
                },
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            },
        }

    def transact_decrement_like_count(self, post_id, like_status):
        if like_status == LikeStatus.ONYMOUSLY_LIKED:
            like_count_attribute = 'onymousLikeCount'
        elif like_status == LikeStatus.ANONYMOUSLY_LIKED:
            like_count_attribute = 'anonymousLikeCount'
        else:
            raise exceptions.PostException(f'Unrecognized like status `{like_status}`')

        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD #count_name :negative_one',
                'ExpressionAttributeValues': {
                    ':negative_one': {'N': '-1'},
                    ':zero': {'N': '0'},
                },
                'ExpressionAttributeNames': {
                    '#count_name': like_count_attribute,
                },
                # only updates and no going below zero
                'ConditionExpression': 'attribute_exists(partitionKey) and #count_name > :zero',
            },
        }

    def transact_increment_comment_count(self, post_id):
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD commentCount :one',
                'ExpressionAttributeValues': {
                    ':one': {'N': '1'},
                },
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            },
        }

    def transact_decrement_comment_count(self, post_id):
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'ADD commentCount :negative_one',
                'ExpressionAttributeValues': {
                    ':negative_one': {'N': '-1'},
                    ':zero': {'N': '0'},
                },
                # only updates and no going below zero
                'ConditionExpression': 'attribute_exists(partitionKey) and commentCount > :zero',
            },
        }

    def transact_set_album_id(self, post_item, album_id, album_rank=None):
        post_id = post_item['postId']
        post_status = post_item['postStatus']

        assert (album_rank is not None) is bool(album_id and post_status == PostStatus.COMPLETED), \
            'album_rank must be specified only when setting album_id for a completed post'
        album_rank = album_rank if album_rank is not None else -1

        transact_item = {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            },
        }
        if album_id:
            transact_item['Update']['UpdateExpression'] = (
                'SET albumId = :aid, gsiK3PartitionKey = :pk, gsiK3SortKey = :ar'
            )
            transact_item['Update']['ExpressionAttributeValues'] = {
                ':aid': {'S': album_id},
                ':pk': {'S': f'post/{album_id}'},
                ':ar': {'N': str(album_rank)},
                ':ps': {'S': post_status},
            }
            transact_item['Update']['ConditionExpression'] += ' and postStatus = :ps'
        else:
            transact_item['Update']['UpdateExpression'] = 'REMOVE albumId, gsiK3PartitionKey, gsiK3SortKey'
        return transact_item

    def transact_set_album_rank(self, post_id, album_rank):
        return {
            'Update': {
                'Key': {
                    'partitionKey': {'S': f'post/{post_id}'},
                    'sortKey': {'S': '-'},
                },
                'UpdateExpression': 'SET gsiK3SortKey = :ar',
                'ExpressionAttributeValues': {':ar': {'N': str(album_rank)}},
                'ConditionExpression': 'attribute_exists(partitionKey)',  # only updates, no creates
            }
        }

    def generate_post_ids_in_album(self, album_id, completed=None, after_rank=None):
        assert completed is None or after_rank is None, 'Cant specify both completed and after_rank kwargs'

        key_exps = [Key('gsiK3PartitionKey').eq(f'post/{album_id}')]
        if completed is True:
            key_exps.append(Key('gsiK3SortKey').gt(-1))
        if completed is False:
            key_exps.append(Key('gsiK3SortKey').eq(-1))
        if after_rank is not None:
            key_exps.append(Key('gsiK3SortKey').gt(after_rank))

        query_kwargs = {
            'KeyConditionExpression': reduce(lambda a, b: a & b, key_exps),
            'IndexName': 'GSI-K3',
            'ProjectionExpression': 'partitionKey',
        }
        return map(
            lambda item: item['partitionKey'].split('/')[1],
            self.client.generate_all_query(query_kwargs),
        )
